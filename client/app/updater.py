"""Phase 5.4 · ``--mode=updater`` 入口。

由 [update_pipeline.py] 的 ``_default_spawn_updater`` spawn 的独立进程跑。
**不**继承 agent 主路径任何 import；从命令行参数自包含运行。

职责（与设计稿 §6 一致）：
1. wait_for_old_agent_exit       让旧 EmployeeAgent.exe 完全退出，释放文件锁
2. backup_old_version             ``install_dir → data_dir/updates/rollback/<old>``
3. atomic_install                 ``stage_dir/EmployeeAgent → install_dir``（先 .new 再 rename）
4. start_new_agent                ``Start-ScheduledTask -TaskName <task_name>``
5. cleanup                        删 stage + 删 .zip + 清理超额 rollback
6. retain 最近 2 个 rollback（设计稿 §10）

失败模式：
- backup 失败 → 中止，旧 install_dir 不动
- copy 失败 → 试图回滚 backup → install_dir 恢复成旧版
- 全失败 → log + 退出非 0；task scheduler 会在 next logon 再起，旧版仍可用

日志写到 ``data_dir/updates/updater.log``，与 agent.log 隔离避免冲突。
"""
from __future__ import annotations

import argparse
import logging
import os
import shutil
import subprocess
import sys
import time
from logging.handlers import RotatingFileHandler
from pathlib import Path

logger = logging.getLogger("updater")

# 最大 rollback 备份数；老的自动清理（设计稿决策）
_RETAIN_ROLLBACKS = 2


def _setup_log(data_dir_path: Path) -> None:
    """updater 自己的 log 文件；与 agent.log 隔离。"""
    log_dir = data_dir_path / "updates"
    try:
        log_dir.mkdir(parents=True, exist_ok=True)
    except OSError:
        pass
    log_file = log_dir / "updater.log"
    handler = RotatingFileHandler(
        log_file, maxBytes=512_000, backupCount=2, encoding="utf-8"
    )
    handler.setFormatter(
        logging.Formatter("%(asctime)s [%(levelname)s] updater: %(message)s")
    )
    logger.setLevel(logging.INFO)
    logger.handlers.clear()
    logger.addHandler(handler)
    # 也输出到 stderr（spawn 时 DETACHED 看不见，但开发期手动启 updater 能看）
    logger.addHandler(logging.StreamHandler())


def _list_agent_pids_other_than_self() -> list[int]:
    """列所有 EmployeeAgent.exe 进程 PID，**排除 updater 自己**。

    关键 fix（v0.5.4 实测发现）：updater 本身就是 EmployeeAgent.exe（D1 同 binary
    --mode=updater），按 IMAGENAME 过滤会把自己也算进去。必须按 PID 排除自己。
    """
    self_pid = os.getpid()
    try:
        out = subprocess.check_output(
            ["tasklist", "/FI", "IMAGENAME eq EmployeeAgent.exe",
             "/FO", "CSV", "/NH"],
            text=True, stderr=subprocess.DEVNULL, timeout=5,
        )
    except subprocess.SubprocessError:
        return []
    pids: list[int] = []
    for line in out.strip().splitlines():
        # CSV: "EmployeeAgent.exe","12345","Console","1","..."
        parts = line.strip().split(",")
        if len(parts) >= 2:
            raw = parts[1].strip().strip('"')
            try:
                pid = int(raw)
            except ValueError:
                continue
            if pid != self_pid:
                pids.append(pid)
    return pids


def _wait_for_agent_exit(timeout: int = 30) -> bool:
    """轮询其它 EmployeeAgent 进程消失（自己不计）。

    超时未消失 → 返回 False，updater 应当显式 kill 兜底。
    """
    deadline = time.time() + timeout
    while time.time() < deadline:
        others = _list_agent_pids_other_than_self()
        if not others:
            return True
        time.sleep(1)
    return False


def _force_kill_agent() -> None:
    """超时兜底：硬杀 *其它* EmployeeAgent 进程（**不杀自己**）。

    用 PID 杀而不是 IMAGENAME，避免把 updater 自己一起干掉。
    """
    for pid in _list_agent_pids_other_than_self():
        try:
            subprocess.run(
                ["taskkill", "/F", "/PID", str(pid)],
                check=False, timeout=5,
            )
        except subprocess.SubprocessError:
            pass


def _backup(install_dir: Path, rollback_root: Path, old_version: str) -> Path:
    """整目录 copy 到 rollback/<old_version>/。"""
    dst = rollback_root / old_version
    if dst.exists():
        shutil.rmtree(dst)
    shutil.copytree(install_dir, dst)
    return dst


def _rmtree_retry(path: Path, attempts: int = 10, wait_seconds: float = 1.0) -> bool:
    """Windows 上 rmtree 常因 OS 异步释放 mapped file handle 撞 SHARING VIOLATION。
    带退避重试；全部失败返回 False。"""
    for _ in range(attempts):
        try:
            if not path.exists():
                return True
            shutil.rmtree(path)
            return True
        except OSError:
            time.sleep(wait_seconds)
    return False


def _atomic_install(stage_dir: Path, install_dir: Path) -> None:
    """``stage_dir/EmployeeAgent → install_dir``，**rename-swap atomic**。

    关键 fix（v0.5.4 实测发现）：先前用 ``rmtree install_dir`` + copy 的方式在
    PyInstaller onedir 场景下经常撞 ``SHARING VIOLATION`` —— agent 进程虽然已
    exit，但 OS 异步释放 ``_internal/*.pyd`` 的 file handle 要几百 ms 到几秒，
    那段时间内目录无法 rmtree。

    新做法（与 Microsoft Update / Chrome auto-update 同思路）：
    1. 复制 stage → ``install_dir.new``（slow，但 OK，与 install_dir 无关）
    2. **rename** ``install_dir → install_dir.old``（atomic，只动 dirent，
       不要求 dir 内文件无 handle）
    3. **rename** ``install_dir.new → install_dir``（atomic）
    4. best-effort 清 ``install_dir.old``（带 retry，全失败留待下次清理）

    Windows ``rename(directory)`` 即使 dir 内部 *.pyd 有 mapped handle 也能成功，
    因为只动父目录的 directory entry。
    """
    src_payload = stage_dir / "EmployeeAgent"
    if not src_payload.is_dir():
        raise FileNotFoundError(f"stage payload missing: {src_payload}")

    new_dir = install_dir.with_name(install_dir.name + ".new")
    old_dir = install_dir.with_name(install_dir.name + ".old")

    # 清前一次失败留下的残留（带 retry，因为可能仍有 handle 占着）
    for leftover in (new_dir, old_dir):
        if leftover.exists():
            if not _rmtree_retry(leftover, attempts=3, wait_seconds=0.5):
                logger.warning("旧残留 %s 暂未清掉，继续", leftover)

    # 1. 复制到 .new
    shutil.copytree(src_payload, new_dir)

    # 2. atomic rename install_dir → .old
    if install_dir.exists():
        for attempt in range(15):
            try:
                install_dir.rename(old_dir)
                break
            except OSError as e:
                if attempt == 14:
                    raise OSError(
                        f"无法 rename {install_dir} 到 .old（{e}）"
                    )
                time.sleep(1)

    # 3. atomic rename .new → install_dir
    new_dir.rename(install_dir)

    # 4. best-effort 清 .old；不阻塞升级结果（OS 释放 handle 后下次再清）
    if not _rmtree_retry(old_dir, attempts=10, wait_seconds=1.0):
        logger.warning(
            ".old cleanup 暂未完成（OS 仍持有 file handles），下次 update 自动清理"
        )


def _rollback_from_backup(backup_dir: Path, install_dir: Path) -> bool:
    """install 失败时把 backup 复制回 install_dir。

    兼容新 atomic_install (rename-swap) 可能留下的三种半成品状态：
    A. ``install_dir.new`` 在，``install_dir`` 完好（rename install_dir → .old 这步失败）
       → 删 .new；install_dir 仍是旧版，正常
    B. ``install_dir.old`` 在，``install_dir`` 不在（swap 半途，new.rename 这步失败）
       → 把 .old rename 回 install_dir，秒回旧版
    C. install_dir 半破（旧 rmtree 风格残留）→ 强删 + copy backup
    """
    new_dir = install_dir.with_name(install_dir.name + ".new")
    old_dir = install_dir.with_name(install_dir.name + ".old")

    # 任何情况都先清 .new 残留
    if new_dir.exists():
        _rmtree_retry(new_dir, attempts=5)

    # case A: install_dir 仍完好（rmtree 没动它）
    if install_dir.exists() and not old_dir.exists() and any(install_dir.iterdir()):
        logger.info("rollback case A: install_dir 完好，无需 copy")
        return True

    # case B: install_dir 不在 + old 在 → rename 回去
    if old_dir.exists() and not install_dir.exists():
        try:
            old_dir.rename(install_dir)
            logger.info("rollback case B: rename .old → install_dir")
            return True
        except OSError:
            logger.exception("case B rename 失败，fall through to copy")

    # case C: install_dir 半破或不存在 → 从 backup 复制
    if not backup_dir.is_dir():
        return False
    try:
        if install_dir.exists():
            if not _rmtree_retry(install_dir, attempts=5):
                return False
        shutil.copytree(backup_dir, install_dir)
        logger.info("rollback case C: copytree backup → install_dir")
        return True
    except OSError:
        logger.exception("rollback 自身失败")
        return False


def _start_new_agent(task_name: str) -> bool:
    """Start-ScheduledTask 触发新 agent 起来。"""
    try:
        subprocess.run(
            ["powershell", "-NoProfile", "-Command",
             f"Start-ScheduledTask -TaskName {task_name}"],
            check=False, timeout=15,
        )
        return True
    except subprocess.SubprocessError:
        logger.exception("Start-ScheduledTask 失败")
        return False


def _cleanup(stage_dir: Path, data_dir_path: Path, new_version: str) -> None:
    """删 stage + 删 incoming/.zip + 收缩 rollback 到最近 N 个。"""
    shutil.rmtree(stage_dir, ignore_errors=True)
    incoming = data_dir_path / "updates" / "incoming" / f"{new_version}.zip"
    try:
        if incoming.exists():
            incoming.unlink()
    except OSError:
        pass

    # 保留最近 N 个 rollback
    rb_root = data_dir_path / "updates" / "rollback"
    if rb_root.is_dir():
        backups = sorted(
            [p for p in rb_root.iterdir() if p.is_dir()],
            key=lambda p: p.stat().st_mtime,
            reverse=True,
        )
        for stale in backups[_RETAIN_ROLLBACKS:]:
            shutil.rmtree(stale, ignore_errors=True)


# ---------------------------------------------------------------------------
# entry point
# ---------------------------------------------------------------------------


def _parse_args(argv: list[str]) -> argparse.Namespace:
    parser = argparse.ArgumentParser(prog="EmployeeAgent --mode=updater")
    parser.add_argument("--mode", required=True)  # 仅占位让 dispatch 看到
    parser.add_argument("--stage-dir", required=True)
    parser.add_argument("--install-dir", required=True)
    parser.add_argument("--data-dir", required=True)
    parser.add_argument("--old-version", required=True)
    parser.add_argument("--new-version", required=True)
    parser.add_argument("--task-name", default="EmployeeAgent")
    return parser.parse_args(argv[1:])


def run_updater(argv: list[str]) -> int:
    """供 main.py 在 ``--mode=updater`` 路径调用。

    :return: 进程退出码（0 成功，1 失败）
    """
    args = _parse_args(argv)
    stage_dir = Path(args.stage_dir)
    install_dir = Path(args.install_dir)
    data_dir_path = Path(args.data_dir)

    # **关键 fix（v0.5.4 实测发现）**：updater 是被 agent spawn 的，继承 agent 的
    # cwd —— Phase 5.1 R3 把 task WorkingDirectory 钉在 install_dir，
    # 于是 updater 进程 cwd = install_dir。
    # Windows 不允许 rename 一个进程的 cwd 所在目录，导致 _atomic_install 的
    # ``install_dir.rename(.old)`` 永远撞 SHARING VIOLATION (WinError 32).
    # 立即 chdir 到 data_dir（保证存在 + 不在 install_dir 下），让 install_dir
    # 没有任何 cwd 引用。
    for candidate in (data_dir_path, Path("C:\\")):
        try:
            os.chdir(candidate)
            break
        except OSError:
            continue

    _setup_log(data_dir_path)
    logger.info(
        "updater started: old=%s new=%s install_dir=%s cwd=%s",
        args.old_version, args.new_version, install_dir, os.getcwd(),
    )

    # 1) wait for old agent
    if not _wait_for_agent_exit(timeout=30):
        logger.warning("agent didn't exit in 30s, force kill")
        _force_kill_agent()
        time.sleep(2)

    # 2) backup
    rollback_root = data_dir_path / "updates" / "rollback"
    try:
        rollback_root.mkdir(parents=True, exist_ok=True)
        backup_dir = _backup(install_dir, rollback_root, args.old_version)
        logger.info("backup OK → %s", backup_dir)
    except Exception:  # noqa: BLE001
        logger.exception("backup 失败，中止升级")
        return 1

    # 3) atomic install
    try:
        _atomic_install(stage_dir, install_dir)
        logger.info("install OK")
    except Exception:  # noqa: BLE001
        logger.exception("install 失败，尝试回滚 backup")
        if _rollback_from_backup(backup_dir, install_dir):
            logger.info("rollback OK")
        else:
            logger.error("rollback 也失败 — install_dir 可能半破，需要人工 rollback.ps1")
        return 1

    # 4) start new agent
    if not _start_new_agent(args.task_name):
        logger.warning("Start-ScheduledTask 失败，agent 可能要等下次 logon")

    # 5) cleanup
    try:
        _cleanup(stage_dir, data_dir_path, args.new_version)
        logger.info("cleanup OK")
    except Exception:  # noqa: BLE001
        logger.exception("cleanup 失败（不影响升级结果）")

    logger.info(
        "updater done: %s → %s", args.old_version, args.new_version
    )
    return 0

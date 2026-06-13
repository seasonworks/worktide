"""Phase 5.4 · update pipeline（checker + downloader + installer 三合一）。

为什么三合一不三分文件：三个阶段在**同一线程**里串行执行（避免并发同 zip
下载、staged 状态切换），共享 UpdateState；拆成三类多文件会引入大量
跨模块引用而无收益。

线程模型：自带 daemon 线程，周期 ``check_interval_seconds`` 触发一次完整
check → download → stage → install 链路。任一步失败 → ``UpdateState.FAILED``
+ log error，下个周期重新进入 ``CHECKING``。

外部依赖（注入）：
- ``UpdateState`` 共享状态对象（与 HealthReporter 共读）
- ``LifecycleRecorder`` 在最终 sys.exit 前写 update_pending
- 实际网络层用 ``requests.Session``，可注入用作测试

**install 触发条件**：staged 验证通过后**立即**触发（D3 决策），不等用户
空闲、不等下次重启。
"""
from __future__ import annotations

import hashlib
import hmac
import logging
import os
import shutil
import subprocess
import sys
import threading
import time
import zipfile
from pathlib import Path
from typing import Callable, Optional

import requests

from . import semver
from . import update_state as us
from .config import UpdateConfig, data_dir
from .lifecycle import EXIT_REASON_UPDATE_PENDING, LifecycleRecorder
from .version import AGENT_VERSION

logger = logging.getLogger(__name__)


# Phase 6.1B · audit log: one JSONL row per cycle attempt (data_dir/state/update_history.jsonl)
def _audit_log_path() -> Path:
    """返回 update audit log 路径；目录创建失败则退回 data_dir 根。"""
    p = data_dir() / "state"
    try:
        p.mkdir(parents=True, exist_ok=True)
    except OSError:
        p = data_dir()
    return p / "update_history.jsonl"


def _audit_append(event: dict) -> None:
    """Append-only JSONL；写失败仅 WARN，不影响主路径。"""
    import json as _json
    from datetime import datetime as _dt, timezone as _tz
    event = dict(event)
    event.setdefault("at", _dt.now(_tz.utc).strftime("%Y-%m-%dT%H:%M:%SZ"))
    event.setdefault("agent_version", AGENT_VERSION)
    try:
        with _audit_log_path().open("a", encoding="utf-8") as f:
            f.write(_json.dumps(event, ensure_ascii=False, sort_keys=True) + "\n")
    except OSError as e:
        logger.warning("update audit log 写失败: %s", e)


# ---------------------------------------------------------------------------
# 路径
# ---------------------------------------------------------------------------


def _updates_root() -> Path:
    p = data_dir() / "updates"
    p.mkdir(parents=True, exist_ok=True)
    return p


def _incoming_path(version: str) -> Path:
    p = _updates_root() / "incoming"
    p.mkdir(parents=True, exist_ok=True)
    return p / f"{version}.zip"


def _staged_dir(version: str) -> Path:
    return _updates_root() / "staged" / version


def _rollback_root() -> Path:
    p = _updates_root() / "rollback"
    p.mkdir(parents=True, exist_ok=True)
    return p


# ---------------------------------------------------------------------------
# 校验工具（独立函数 + 可单测）
# ---------------------------------------------------------------------------


def verify_sha256(zip_path: Path, expected_hex: str) -> bool:
    """对比 zip 的 sha256 与 manifest 期望值。"""
    h = hashlib.sha256()
    try:
        with zip_path.open("rb") as f:
            for chunk in iter(lambda: f.read(1024 * 1024), b""):
                h.update(chunk)
    except OSError:
        return False
    return h.hexdigest().lower() == expected_hex.lower()


def verify_hmac(version: str, sha256: str, size: int, signature: str,
                secret: str) -> bool:
    """HMAC-SHA256(secret, f"{version}|{sha256}|{size}") 与 signature 对比。

    与 [server/app/services/release_manager.py] ``compute_hmac_signature``
    canonical 字符串必须严格一致；改一处要双向同步并 bump min_compat_version。
    """
    if not secret:
        # 没配 secret 意味着不校验。安全默认：拒绝。
        return False
    canonical = f"{version}|{sha256}|{size}".encode("utf-8")
    expected = hmac.new(secret.encode("utf-8"), canonical, hashlib.sha256).hexdigest()
    # constant-time compare 防 timing attack（虽然 client side 几乎无意义）
    return hmac.compare_digest(expected.lower(), signature.lower())


# ---------------------------------------------------------------------------
# UpdatePipeline
# ---------------------------------------------------------------------------


class UpdatePipeline:
    """周期 check + 完整 download/stage/install 链路。"""

    # install 时调用的"agent exit hook"。默认 os._exit；测试可注入 raise SystemExit
    EXIT_CODE = 0  # 与 watchdog 的 EXIT_CODE=2 区分：update 是正常 exit，不让 task 重启

    def __init__(
        self,
        config: UpdateConfig,
        state: us.UpdateState,
        lifecycle: LifecycleRecorder,
        *,
        machine_id: str,
        server_base_url: str,
        request_timeout_seconds: int = 30,
        session: Optional[requests.Session] = None,
        # 测试钩子
        exit_callable: Callable[[int], None] = os._exit,
        spawn_updater: Optional[Callable[..., None]] = None,
        health_post_url: Optional[str] = None,
        health_post_payload_builder: Optional[Callable[[], dict]] = None,
    ) -> None:
        self.config = config
        self.state = state
        self.lifecycle = lifecycle
        self.machine_id = machine_id
        self.server_base_url = server_base_url.rstrip("/")
        self.timeout = max(5, int(request_timeout_seconds))
        self.session = session or requests.Session()

        self._exit_callable = exit_callable
        self._spawn_updater = spawn_updater or self._default_spawn_updater
        self._health_post_url = health_post_url
        self._health_post_payload_builder = health_post_payload_builder

        self._stop = threading.Event()
        self._thread: Optional[threading.Thread] = None

    # ------------------------------------------------------------------
    # 生命周期
    # ------------------------------------------------------------------

    def start(self) -> None:
        if not self.config.enabled:
            logger.info("update pipeline 未启用 (config.update.enabled=False)")
            return
        if self._thread and self._thread.is_alive():
            return
        self._stop.clear()
        self._thread = threading.Thread(
            target=self._run, name="update-pipeline", daemon=True
        )
        self._thread.start()
        logger.info(
            "update pipeline 启动 check_interval=%ds channel=%s version=%s",
            self.config.check_interval_seconds,
            self.config.channel,
            AGENT_VERSION,
        )

    def stop(self) -> None:
        self._stop.set()
        if self._thread:
            self._thread.join(timeout=10)
        logger.info("update pipeline 已停止")

    # ------------------------------------------------------------------
    # 主循环
    # ------------------------------------------------------------------

    def _run(self) -> None:
        # 启动后给 health_reporter 几秒先报第一次（避免 client 还没注册 device_health
        # 行就 check）
        self._stop.wait(self.config.initial_delay_seconds)
        while not self._stop.is_set():
            try:
                self._cycle()
            except Exception:  # noqa: BLE001  pipeline 永远不打死自己
                logger.exception("update cycle 异常")
                self.state.transition(us.FAILED, error="pipeline_exception")
            self._stop.wait(self.config.check_interval_seconds)

    # ------------------------------------------------------------------
    # 单次完整链路（check → download → stage → install）
    # ------------------------------------------------------------------

    def _cycle(self) -> None:
        # 1) check
        self.state.transition(us.CHECKING)
        self.state.mark_check_attempted()
        manifest = self._check_remote()
        if manifest is None:
            # 没有 update 可用 / 检查失败 → 回 idle 等下个周期
            # Phase 6.1B: 即使没 manifest 也要 audit 一行 — 否则灰度时
            # "update_pipeline 是不是在跑"完全没法验证（这次 dogfood 就踩到）。
            # 区分"服务端说无更新"（最常见，相当于服务端 guard 拦截）与
            # "网络/解析失败"两种情况由 _check_remote 内部 log 区分。
            _audit_append({
                "event": "check_completed",
                "current_version": AGENT_VERSION,
                "target_version": None,
                "result": "no_update",
                "machine_id": self.machine_id,
            })
            self.state.transition(us.IDLE)
            return

        target_version: str = manifest["version"]

        # ------------------------------------------------------------------
        # Phase 6.1B · ANTI-DOWNGRADE GUARD
        #
        # Bug 历史：6.0D-V 时一台测试机 0.6.1 自降级到 0.6.0 — 原因是 server 拿到
        # 任何 `latest != current` 都发 manifest，client 无脑装。dogfood agent.log
        # 16:01 那一段 idle→checking→downloading target=0.6.0 是直接证据。
        # 同 bug 也被怀疑是过往"装 0.6.0 显示 0.5.4"现象的合理解释（latest.json
        # 在 Cloudflare 缓存抖动期间短暂指向 0.5.4）。
        #
        # 守卫规则：target 必须**严格大于** current，否则记 WARNING、写 audit、
        # 回 IDLE。不进入下载 / staged / installing 任何阶段。
        # ------------------------------------------------------------------
        if not semver.is_upgrade(AGENT_VERSION, target_version):
            cmp = semver.compare(target_version, AGENT_VERSION)
            reason = "same_version" if cmp == 0 else "downgrade_blocked"
            logger.warning(
                "update %s · current=%s target=%s",
                reason, AGENT_VERSION, target_version,
            )
            _audit_append({
                "event": "check_completed",
                "current_version": AGENT_VERSION,
                "target_version": target_version,
                "result": reason,
                "manifest_sha256": manifest.get("sha256"),
                "manifest_size": manifest.get("size"),
                "machine_id": self.machine_id,
            })
            # same_version 是正常稳定态，不算"失败"；downgrade_blocked 也是 client
            # 主动拒绝，state 回 idle，下个 cycle 重新 check（万一服务端这期间
            # 翻了 latest.json 到更新的版本）。两者都不写 last_error。
            self.state.transition(us.IDLE)
            return

        # 升级路径合法 — 入 audit 然后进入下载
        _audit_append({
            "event": "upgrade_started",
            "current_version": AGENT_VERSION,
            "target_version": target_version,
            "result": "started",
            "manifest_sha256": manifest.get("sha256"),
            "manifest_size": manifest.get("size"),
            "machine_id": self.machine_id,
        })

        # 2) download
        self.state.transition(us.DOWNLOADING, target_version=target_version)
        zip_path = self._download(manifest)
        if zip_path is None:
            _audit_append({
                "event": "download_failed", "current_version": AGENT_VERSION,
                "target_version": target_version, "result": "download_failed",
                "machine_id": self.machine_id,
            })
            self.state.transition(us.FAILED, error="download_failed")
            return

        # 3) verify
        if not verify_sha256(zip_path, manifest["sha256"]):
            logger.error("sha256 mismatch for %s", target_version)
            self._safe_unlink(zip_path)
            _audit_append({
                "event": "verify_failed", "current_version": AGENT_VERSION,
                "target_version": target_version, "result": "sha256_mismatch",
                "machine_id": self.machine_id,
            })
            self.state.transition(us.FAILED, error="sha256_mismatch")
            return
        if not verify_hmac(
            target_version, manifest["sha256"], manifest["size"],
            manifest["signature"], self.config.hmac_secret,
        ):
            logger.error("hmac mismatch for %s", target_version)
            self._safe_unlink(zip_path)
            _audit_append({
                "event": "verify_failed", "current_version": AGENT_VERSION,
                "target_version": target_version, "result": "hmac_mismatch",
                "machine_id": self.machine_id,
            })
            self.state.transition(us.FAILED, error="hmac_mismatch")
            return

        # 4) stage
        stage_dir = _staged_dir(target_version)
        if not self._stage(zip_path, stage_dir):
            _audit_append({
                "event": "stage_failed", "current_version": AGENT_VERSION,
                "target_version": target_version, "result": "stage_failed",
                "machine_id": self.machine_id,
            })
            self.state.transition(us.FAILED, error="stage_failed")
            return
        self.state.transition(us.STAGED, target_version=target_version)
        _audit_append({
            "event": "stage_completed", "current_version": AGENT_VERSION,
            "target_version": target_version, "result": "staged",
            "machine_id": self.machine_id,
        })

        # 5) install (D3: staged 后立即安装，不等用户空闲)
        # 安装前必须上报 installing 状态（D3 决策硬性要求）
        self.state.transition(us.INSTALLING, target_version=target_version)
        self._sync_post_health()
        _audit_append({
            "event": "install_initiated", "current_version": AGENT_VERSION,
            "target_version": target_version, "result": "install_initiated",
            "machine_id": self.machine_id,
        })
        self._install(stage_dir, target_version)
        # _install 调 os._exit 后这里不会执行

    # ------------------------------------------------------------------
    # check
    # ------------------------------------------------------------------

    def _check_remote(self) -> Optional[dict]:
        url = f"{self.server_base_url}/api/v1/agent/updates/check"
        try:
            resp = self.session.get(
                url,
                params={
                    "current_version": AGENT_VERSION,
                    "channel": self.config.channel,
                    "machine_id": self.machine_id,
                },
                timeout=self.timeout,
            )
        except requests.RequestException as e:
            logger.warning("update check 网络异常：%s", e)
            return None
        if resp.status_code != 200:
            logger.warning("update check HTTP %d", resp.status_code)
            return None
        try:
            data = resp.json()
        except ValueError:
            logger.warning("update check 响应非 JSON")
            return None
        if not data.get("update_available"):
            return None
        manifest = data.get("manifest")
        if not manifest:
            return None
        # 必填字段最小校验；缺失则视为非法 manifest
        for k in ("version", "sha256", "signature", "size", "download_url"):
            if k not in manifest:
                logger.warning("manifest 字段缺失 %s", k)
                return None
        return manifest

    # ------------------------------------------------------------------
    # download (流式 + 边下边算 sha256)
    # ------------------------------------------------------------------

    def _download(self, manifest: dict) -> Optional[Path]:
        version = manifest["version"]
        download_url = manifest["download_url"]
        # 相对路径 → 拼 server_base_url
        if download_url.startswith("/"):
            url = self.server_base_url + download_url
        else:
            url = download_url

        final_path = _incoming_path(version)
        partial_path = final_path.with_suffix(".zip.partial")
        # 清旧 partial（中断恢复 v1 不做，从头下）
        self._safe_unlink(partial_path)

        try:
            with self.session.get(url, stream=True, timeout=self.timeout) as resp:
                if resp.status_code != 200:
                    logger.warning("download HTTP %d", resp.status_code)
                    return None
                with partial_path.open("wb") as f:
                    for chunk in resp.iter_content(chunk_size=1024 * 64):
                        if self._stop.is_set():
                            logger.info("download 中止：stop 信号")
                            return None
                        if chunk:
                            f.write(chunk)
        except (requests.RequestException, OSError) as e:
            logger.warning("download 失败：%s", e)
            self._safe_unlink(partial_path)
            return None

        # atomic rename → final
        try:
            os.replace(partial_path, final_path)
        except OSError as e:
            logger.warning("download 完成但 rename 失败：%s", e)
            return None
        return final_path

    # ------------------------------------------------------------------
    # stage (unzip + 检查 entry point)
    # ------------------------------------------------------------------

    def _stage(self, zip_path: Path, stage_dir: Path) -> bool:
        # 清旧 stage（重复升级同版本时）
        if stage_dir.exists():
            shutil.rmtree(stage_dir, ignore_errors=True)
        stage_dir.mkdir(parents=True, exist_ok=True)
        try:
            with zipfile.ZipFile(zip_path) as zf:
                zf.extractall(stage_dir)
        except zipfile.BadZipFile as e:
            logger.error("zip 解压失败：%s", e)
            shutil.rmtree(stage_dir, ignore_errors=True)
            return False
        # entry point 校验：staged_dir/EmployeeAgent/EmployeeAgent.exe 必须存在
        entry = stage_dir / "EmployeeAgent" / "EmployeeAgent.exe"
        if not entry.is_file():
            logger.error("staged entry point missing: %s", entry)
            return False
        return True

    # ------------------------------------------------------------------
    # install (spawn updater + agent self-exit)
    # ------------------------------------------------------------------

    def _install(self, stage_dir: Path, new_version: str) -> None:
        install_dir = self._resolve_install_dir()
        if not install_dir:
            logger.error("install_dir 推断失败，跳过 install")
            self.state.transition(us.FAILED, error="install_dir_unknown")
            return
        try:
            self._spawn_updater(
                stage_dir=stage_dir,
                install_dir=install_dir,
                data_dir_path=data_dir(),
                old_version=AGENT_VERSION,
                new_version=new_version,
            )
        except Exception as e:  # noqa: BLE001
            logger.exception("spawn updater 失败")
            self.state.transition(us.FAILED, error=f"spawn_failed:{e}")
            return
        # agent 立即退出，让 updater 接管
        try:
            self.lifecycle.record_exit(EXIT_REASON_UPDATE_PENDING)
        except Exception:  # noqa: BLE001
            logger.exception("record_exit(update_pending) 失败")
        logger.info("agent exit for updater takeover (target=%s)", new_version)
        # 关键：用 exit_callable（默认 os._exit(0)）立即终止整个进程
        # 不能 sys.exit —— 在 daemon 线程里只 raise 不退主线程
        self._exit_callable(self.EXIT_CODE)

    def _resolve_install_dir(self) -> Optional[Path]:
        """frozen → EXE 同目录；源码 → 仍指向 EXE 路径（开发/测试场景）。

        对源码运行，install 本身没意义（python main.py 没法被 updater 替换），
        但仍返回一个合理路径让 spawn_updater 不挂；updater 实际不会改源码目录
        （由 updater 自己的安全检查兜底）。
        """
        if getattr(sys, "frozen", False):
            return Path(sys.executable).resolve().parent
        # 源码：返回 client/ 下一个虚拟路径，让 spawn_updater 仍能被调用
        return Path(sys.executable).resolve().parent

    def _default_spawn_updater(
        self, *, stage_dir: Path, install_dir: Path, data_dir_path: Path,
        old_version: str, new_version: str,
    ) -> None:
        """启 staged/<ver>/EmployeeAgent/EmployeeAgent.exe --mode=updater。"""
        staged_exe = stage_dir / "EmployeeAgent" / "EmployeeAgent.exe"
        args = [
            str(staged_exe),
            "--mode=updater",
            f"--stage-dir={stage_dir}",
            f"--install-dir={install_dir}",
            f"--data-dir={data_dir_path}",
            f"--old-version={old_version}",
            f"--new-version={new_version}",
            "--task-name=EmployeeAgent",
        ]
        # DETACHED_PROCESS + CREATE_NEW_PROCESS_GROUP：updater 完全脱离 agent
        # 当 agent 进程退出后，updater 继续跑
        creationflags = 0
        if sys.platform == "win32":
            DETACHED_PROCESS = 0x00000008  # noqa: N806
            CREATE_NEW_PROCESS_GROUP = 0x00000200  # noqa: N806
            creationflags = DETACHED_PROCESS | CREATE_NEW_PROCESS_GROUP
        # cwd 必须**不在** install_dir 下，否则 updater 进程继承的 cwd 会让
        # Windows 拒绝 rename install_dir（实测 v0.5.4 撞过这个，hotfix）。
        # data_dir_path（=ProgramData）保证存在且与 install_dir 解耦。
        subprocess.Popen(
            args,
            creationflags=creationflags,
            close_fds=True,
            cwd=str(data_dir_path),
        )
        # 让 updater 抢一小段 CPU 起线程，再让本进程退（防止 race）
        time.sleep(0.5)

    # ------------------------------------------------------------------
    # 辅助
    # ------------------------------------------------------------------

    @staticmethod
    def _safe_unlink(p: Path) -> None:
        try:
            if p.exists():
                p.unlink()
        except OSError:
            pass

    def _sync_post_health(self) -> None:
        """D3 强制：install 前同步上报 installing 状态。

        HealthReporter 自己是周期 120s，等不到下个周期。这里 fire-and-forget
        一次 POST，失败也不阻塞 install（updater 仍会接管）。
        """
        if not self._health_post_url or not self._health_post_payload_builder:
            return
        try:
            payload = self._health_post_payload_builder()
            self.session.post(self._health_post_url, json=payload, timeout=10)
        except Exception:  # noqa: BLE001
            logger.exception("installing 状态上报失败（不阻塞 install）")

"""客户端入口：初始化日志，加载配置，启动上报循环。

源码运行：
    python main.py

打包 EXE 后还可以分两种 mode：
- 默认（无 --mode）→ agent
- --mode=updater  → 升级器（Phase 5.4），独立进程接管文件替换

后期打包 EXE / 静默运行 / 开机自启 / 系统托盘将在此基础上扩展。
"""
import logging
import sys
from logging.handlers import RotatingFileHandler

from app.agent import Agent
from app.config import data_dir, load_config
from app.lifecycle import (
    EXIT_REASON_CRASH,
    EXIT_REASON_INITIAL,
    EXIT_REASON_NORMAL,
    LifecycleRecorder,
)
from app.single_instance import SingleInstance
from app.watchdog import Watchdog, make_heartbeat

# Phase 6.1A · 启动早期标记上次异常退出。把上次原因显式 log 成 WARNING
# 让现场看 agent.log 的人一眼能看到"为什么上次挂"。normal / initial 走 INFO。
_BENIGN_EXIT_REASONS = {EXIT_REASON_NORMAL, EXIT_REASON_INITIAL, None, ""}

logger = logging.getLogger(__name__)


def setup_logging() -> None:
    # 日志写入可写数据目录；目录创建失败则降级到数据目录根，绝不让启动崩溃
    try:
        log_dir = data_dir() / "logs"
        log_dir.mkdir(parents=True, exist_ok=True)
        log_file = log_dir / "agent.log"
    except OSError:
        log_file = data_dir() / "agent.log"

    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s [%(levelname)s] %(name)s: %(message)s",
        handlers=[
            logging.StreamHandler(),
            RotatingFileHandler(
                log_file, maxBytes=1_000_000, backupCount=3, encoding="utf-8"
            ),
        ],
    )


def main() -> None:
    setup_logging()

    # Phase 5.1：命名 Mutex 防止 task 拉起的 agent 与手动双击 EXE 启的 agent 并发
    # 写 window_buffer.db（SQLite WAL 多写者会竞态）。绑到本地变量 _lock 即可，
    # Agent.run() 阻塞到进程结束 → 引用全程在栈上 → mutex 全程持有。
    _lock = SingleInstance()
    if not _lock.acquire():
        # 注意文案与 Phase 5.1 设计稿一致；运维 / verify 脚本 / 日志监控可能 grep 这串
        logger.warning("another instance detected, exiting")
        return

    # Phase 5.2：lifecycle 记录这次启动；即便 record_start 写盘失败也不影响启动
    lifecycle = LifecycleRecorder()
    try:
        info = lifecycle.record_start()
        logger.info(
            "lifecycle: restart_count=%d last_exit_reason=%s",
            info.get("restart_count"),
            info.get("last_exit_reason"),
        )
        # Phase 6.1A · D: 异常退出原因（watchdog_*_timeout / crash / update_pending）
        # 升 WARNING，便于现场诊断 agent.log 时 grep "WARNING" 即可找到上次故障点。
        last_reason = info.get("last_exit_reason")
        if last_reason not in _BENIGN_EXIT_REASONS:
            logger.warning(
                "上次异常退出：%s   restart_count=%s   last_exit_at=%s",
                last_reason,
                info.get("restart_count"),
                info.get("last_exit_at"),
            )
    except Exception:  # noqa: BLE001  观察通道不阻塞核心路径
        logger.exception("lifecycle.record_start 失败，忽略")

    # 触发过 watchdog 退出后写过 exit_reason 的标志；用于决定 main 退出时
    # 是否覆写为 "normal"。watchdog 的 os._exit 通常不会让我们走到 finally，
    # 但若 exit_callable 被 mock（测试场景）会走到这里。
    exit_recorded = False

    config = load_config()

    # Phase 5.2：根据 config.watchdog.enabled 决定要不要起真心跳 + watchdog 线程
    heartbeat = make_heartbeat(config.watchdog.enabled)
    watchdog = None
    if config.watchdog.enabled:
        try:
            watchdog = Watchdog(config.watchdog, heartbeat, lifecycle)
            watchdog.start()
        except Exception:  # noqa: BLE001  watchdog 起不来不影响 agent
            logger.exception("watchdog 启动失败，agent 仍继续运行（无自检）")
            watchdog = None

    try:
        # Phase 5.3：把 lifecycle + watchdog 都传给 Agent，让它构造 HealthReporter
        # 时可以拿到 lifecycle.snapshot() 和 watchdog.snapshot_for_reporting()
        Agent(
            config,
            heartbeat=heartbeat,
            lifecycle=lifecycle,
            watchdog=watchdog,
        ).run()
        # 正常返回路径（Agent.run() 收到 KeyboardInterrupt 或 stop() 后落 finally 出来）
        try:
            lifecycle.record_exit(EXIT_REASON_NORMAL)
            exit_recorded = True
        except Exception:  # noqa: BLE001
            logger.exception("record_exit(normal) 失败")
    except Exception:  # noqa: BLE001  顶层兜底：致命异常落日志后退出，便于排查
        logger.exception("客户端因未捕获异常退出")
        if not exit_recorded:
            try:
                lifecycle.record_exit(EXIT_REASON_CRASH)
            except Exception:  # noqa: BLE001
                logger.exception("record_exit(crash) 失败")
        raise
    finally:
        # 测试场景下 watchdog 没把进程真杀掉时，让 daemon 线程也优雅停一下
        if watchdog is not None:
            try:
                watchdog.stop()
            except Exception:  # noqa: BLE001
                pass


if __name__ == "__main__":
    # Phase 5.4: --mode=updater 路由到独立 updater 入口（共享同 EXE 二进制，D1 决策）
    if "--mode=updater" in sys.argv:
        from app.updater import run_updater
        sys.exit(run_updater(sys.argv))
    main()

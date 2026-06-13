"""历史记录定期清理。

后台 daemon 线程，不阻塞主线程（请求处理/上报照常）。按保留天数分批删除
过期的 activity_logs，配合 database.py 的 WAL/busy_timeout 防止锁表。
"""
import logging
import threading
from datetime import datetime, timedelta, timezone

from . import crud
from .config import settings
from .database import SessionLocal, checkpoint_truncate
from .services import window_cleanup

logger = logging.getLogger(__name__)


class CleanupService:
    def __init__(self, initial_delay_seconds: int = 10) -> None:
        self._stop = threading.Event()
        self._thread: threading.Thread | None = None
        self._initial_delay = initial_delay_seconds

    def start(self) -> None:
        if self._thread and self._thread.is_alive():
            return
        self._stop.clear()
        self._thread = threading.Thread(
            target=self._run, name="history-cleanup", daemon=True
        )
        self._thread.start()
        logger.info(
            "历史清理线程已启动：保留 %s 天，每 %s 秒清理一次",
            settings.log_retention_days,
            settings.cleanup_interval_seconds,
        )

    def stop(self) -> None:
        self._stop.set()
        if self._thread:
            self._thread.join(timeout=5)
        logger.info("历史清理线程已停止")

    def _run(self) -> None:
        # 初始延迟，避免拖慢启动；期间收到停止信号则直接退出
        if self._stop.wait(self._initial_delay):
            return
        while not self._stop.is_set():
            self.run_once()
            # Event.wait 休眠，可被 stop() 立即唤醒（低 CPU）
            self._stop.wait(settings.cleanup_interval_seconds)

    def run_once(self) -> None:
        """执行一次清理。每类独立 session + 独立 try，互不影响线程存活。"""
        now = datetime.now(timezone.utc)

        # 1) activity_logs（现有，行为完全不变）
        cutoff = now - timedelta(days=settings.log_retention_days)
        db = SessionLocal()
        try:
            deleted = crud.delete_logs_older_than(
                db, cutoff, batch_size=settings.cleanup_batch_size
            )
            if deleted:
                logger.info(
                    "历史清理完成：删除 %s 条早于 %s 的记录",
                    deleted,
                    cutoff.isoformat(),
                )
            else:
                logger.debug("历史清理：无过期记录")
        except Exception:  # noqa: BLE001  清理失败不应影响主服务
            logger.exception("历史清理任务异常")
            db.rollback()
        finally:
            db.close()  # 及时关闭 session

        # 2) window_events_raw（Phase 4.1，retention 配置驱动）
        raw_cutoff = now - timedelta(days=settings.window_raw_retention_days)
        db = SessionLocal()
        try:
            n = window_cleanup.delete_window_events_raw_older_than(
                db, raw_cutoff, batch_size=settings.cleanup_batch_size
            )
            if n:
                logger.info(
                    "窗口 raw 清理完成：删除 %s 条早于 %s 的事件",
                    n,
                    raw_cutoff.isoformat(),
                )
            else:
                logger.debug("窗口 raw 清理：无过期事件")
        except Exception:  # noqa: BLE001
            logger.exception("窗口 raw 清理任务异常")
            db.rollback()
        finally:
            db.close()

        # 3) window_sessions（Phase 4.1）
        sess_cutoff = now - timedelta(days=settings.window_sessions_retention_days)
        db = SessionLocal()
        try:
            n = window_cleanup.delete_window_sessions_older_than(
                db, sess_cutoff, batch_size=settings.cleanup_batch_size
            )
            if n:
                logger.info(
                    "窗口 sessions 清理完成：删除 %s 条早于 %s 的会话",
                    n,
                    sess_cutoff.isoformat(),
                )
            else:
                logger.debug("窗口 sessions 清理：无过期会话")
        except Exception:  # noqa: BLE001
            logger.exception("窗口 sessions 清理任务异常")
            db.rollback()
        finally:
            db.close()

        # 4) WAL 收尾：三轮删除已腾出大量空间，此时 checkpoint(TRUNCATE)
        #    把 WAL 落库并截回，回收磁盘、压低 WAL 高水位（配合 journal_size_limit）
        checkpoint_truncate()


cleanup_service = CleanupService()

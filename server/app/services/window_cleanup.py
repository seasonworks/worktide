"""Phase 4.1 · 窗口活动数据 retention 清理。

由现有 cleanup_service 在 daemon 线程内调用，**不新增线程**。
两类删除独立、参数独立、配置驱动；分批 + 单批 commit + WAL 安全（与
crud.delete_logs_older_than 同模式）。

隐私边界：函数只按时间删除，不读 / 不写 / 不日志 window_title。
"""
import logging
from datetime import datetime

from sqlalchemy import delete, select
from sqlalchemy.orm import Session

from ..models import WindowEventRaw, WindowSession

logger = logging.getLogger(__name__)


def delete_window_events_raw_older_than(
    db: Session, cutoff: datetime, batch_size: int = 5000
) -> int:
    """分批删除 uploaded_at < cutoff 的 raw 事件，返回删除总行数。

    每批单独 commit，避免一次性大删长时间持写锁。WAL 模式下读不阻塞。
    """
    total = 0
    while True:
        ids = db.scalars(
            select(WindowEventRaw.id)
            .where(WindowEventRaw.uploaded_at < cutoff)
            .limit(batch_size)
        ).all()
        if not ids:
            break
        db.execute(delete(WindowEventRaw).where(WindowEventRaw.id.in_(ids)))
        db.commit()
        total += len(ids)
        if len(ids) < batch_size:
            break
    return total


def delete_window_sessions_older_than(
    db: Session, cutoff: datetime, batch_size: int = 5000
) -> int:
    """分批删除 started_at < cutoff 的聚合 session，返回删除总行数。"""
    total = 0
    while True:
        ids = db.scalars(
            select(WindowSession.id)
            .where(WindowSession.started_at < cutoff)
            .limit(batch_size)
        ).all()
        if not ids:
            break
        db.execute(delete(WindowSession).where(WindowSession.id.in_(ids)))
        db.commit()
        total += len(ids)
        if len(ids) < batch_size:
            break
    return total

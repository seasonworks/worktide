"""Phase 6.5A · UX-003 修复：手动 Punch 后 60s 抑制 idle_exit 通知。

机制：
  - telegram_handlers `_execute_punch_action` 在 state 转换成功（result.changed=True）
    后调用 `mark()`。
  - activity 路由准备发 notify_idle_exit 前调用 `is_suppressed()`；True 则跳过推送。

设计取舍：
  - 进程内 dict（与 routers.activity 的 _prev_status 同模式，重启丢失最多多发
    一次 idle_exit）
  - 60s 窗口：覆盖典型 BTW → 客户端 next 上报 → idle→active 翻转的延迟
    （通常 5-30s），同时避免长期压制真正自然回归的 idle_exit
  - mark() 不区分动作类型：clock_in / return_to_work / break_* 均触发，
    因为它们都是"员工刚做了显式操作"的强信号
"""
import logging
import threading
from datetime import datetime, timezone

logger = logging.getLogger(__name__)

SUPPRESS_SECONDS = 60

_lock = threading.Lock()
_last_at: dict[int, datetime] = {}


def mark(employee_id: int, when: datetime | None = None) -> None:
    """记录一次手动 Punch 成功的时间。"""
    when = when or datetime.now(timezone.utc)
    with _lock:
        _last_at[employee_id] = when


def is_suppressed(employee_id: int, now: datetime) -> bool:
    """判断 idle_exit 此刻是否应被抑制。"""
    with _lock:
        last = _last_at.get(employee_id)
    if last is None:
        return False
    delta = (now - last).total_seconds()
    return 0 <= delta <= SUPPRESS_SECONDS


def reset() -> None:
    """smoke / 单测用。生产路径不调。"""
    with _lock:
        _last_at.clear()

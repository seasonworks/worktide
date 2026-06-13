"""Phase 6.5A · BUG-010 修复：自动回座后短时抑制 idle_enter。

机制（与 UX-003 的 manual_punch 同族）：
  - sweeper 在 ON 模式 timeout 自动回座成功后调用 `mark(employee_id)`。
  - activity 路由准备发 notify_idle_enter 前调用 `is_suppressed()`；True 则跳过。

为什么需要：
  break 超时瞬间，sweeper 把仍 AFK 的员工从 break_* 强制翻成 working。紧随其后
  到达的 idle 上报读到的已是 working → is_detection_paused()=False → idle_enter
  触发，与 Break Timeout 撞同一分钟（BUG-010 race）。本模块给"系统强制回座"加一个
  缓冲窗：回座后 SUPPRESS_SECONDS 内不发 idle_enter，给员工回到座位的时间。

设计取舍：
  - 进程内 dict（同 manual_punch / _prev_status 模式；重启清空 → 至多漏抑制一次）
  - 窗口 120s：覆盖 sweeper 60s tick + 客户端 30s 上报周期 + 线程调度抖动的最坏延迟；
    若员工 120s 后仍 AFK，下一轮 idle 上报会正常发 idle_enter，不长期掩盖真实挂机
  - **只抑制 idle_enter**：idle_exit / break_timeout / manual_punch / 其它通知不受影响
"""
import logging
import threading
from datetime import datetime, timezone

logger = logging.getLogger(__name__)

SUPPRESS_SECONDS = 120

_lock = threading.Lock()
_last_at: dict[int, datetime] = {}


def mark(employee_id: int, when: datetime | None = None) -> None:
    """记录一次"系统自动回座"的时间。"""
    when = when or datetime.now(timezone.utc)
    with _lock:
        _last_at[employee_id] = when


def is_suppressed(employee_id: int, now: datetime) -> bool:
    """判断 idle_enter 此刻是否应因近期自动回座被抑制。"""
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

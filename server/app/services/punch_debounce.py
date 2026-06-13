"""Phase 6.5B · UX-004 修复：双击同一打卡按钮 → 吞掉重复的 noop 反馈。

背景：
  员工在群里连点两次「吃饭/Lunch」时，第 1 次成功（🍽 状态卡片），第 2 次状态机
  返回 break_already_active（changed=False）→ Reply Keyboard 路径会再发一条
  "ℹ️ Already On Lunch Break"。成功卡片紧跟一条「已在用餐」对员工像自相矛盾。
  后台无脏数据（work_state 正确），纯属双击 UX。

机制：
  - telegram_handlers `_execute_punch_action` 在动作成功（result.changed=True）后
    调用 `mark(employee_id, action)` 记录(员工, callback_data)的成功时刻。
  - 同一员工对同一 action 在 WINDOW_SECONDS 内再次触发且本次是 noop 时，调用
    `is_recent_duplicate()` 命中 → 静默吞掉这条 noop（不回 "Already On..."）。

设计取舍：
  - 进程内 dict（与 manual_punch / auto_return 同模式，重启丢失最多多发一次 noop）。
  - 8s 窗口：覆盖典型双击/网络卡顿补点（通常 <3s），又不至于压制员工“稍后特意再点
    一次想确认状态”的合理 noop 反馈。
  - 锚定“成功时刻”而非每次按下都续期：只在 changed=True 时 mark，超窗后重复点击仍能
    正常拿到友好提示。
  - 键为 (employee_id, action)：只压制完全相同按钮的重复，切换休息类型（也是 noop?
    不，是 changed=True）不受影响。
  - 只压制 noop，不影响成功卡片、真实异常、inline toast（inline noop 仅 ephemeral
    toast，本就不产生持久双消息）。
"""
import logging
import threading
from datetime import datetime, timezone

logger = logging.getLogger(__name__)

WINDOW_SECONDS = 8

_lock = threading.Lock()
_last_success_at: dict[tuple[int, str], datetime] = {}


def mark(employee_id: int, action: str, when: datetime | None = None) -> None:
    """记录一次(员工, action)成功的时刻。"""
    when = when or datetime.now(timezone.utc)
    with _lock:
        _last_success_at[(employee_id, action)] = when


def is_recent_duplicate(
    employee_id: int, action: str, now: datetime | None = None
) -> bool:
    """同一(员工, action)是否在窗口内刚成功过（→ 本次重复 noop 应被吞掉）。"""
    now = now or datetime.now(timezone.utc)
    with _lock:
        last = _last_success_at.get((employee_id, action))
    if last is None:
        return False
    delta = (now - last).total_seconds()
    return 0 <= delta <= WINDOW_SECONDS


def reset() -> None:
    """smoke / 单测用。生产路径不调。"""
    with _lock:
        _last_success_at.clear()

"""break 超时清扫守护线程（独立于 Telegram，但通过 notifier 触发 GS-3 通知）。

周期调用 work_state.expire_overdue_breaks：debug/API/Telegram 创建的 break 都会被自动超时
恢复。线程异常不崩。仿 cleanup_service 的 Event 等待模式。

Phase 6.5A · GS-3：每个被关闭的超时 break 通过 work_state 的 on_timeout 回调
通知 Telegram 群（"⏰ Break Timeout"）。notifier 内部已自查 notify_break_timeout_enabled
开关，sweeper 这里不再二次检查，避免开关源不一致。
"""
import logging
import threading

from ..config import settings
from ..database import SessionLocal
from ..notifier import notifier
from . import auto_return, work_state
from .settings_service import settings_service

logger = logging.getLogger(__name__)

# GS-13 · OFF 模式去重：已发过"请手动返回"通知的 break_id 集合。
# 进程内 set（同 manual_punch / _prev_status 模式，重启清空 → 至多重发一次）。
# 每个 sweep tick 用当前仍 open 的超时 break id 求交集，自动剔除已被员工手动回座/
# 下班关闭的旧 id，避免无限增长。
_off_mode_notified: set[int] = set()


def _on_break_timeout(employee_id: int, employee_name: str,
                       break_type: str, duration_seconds: int) -> None:
    """GS-3 · 自动回座 ON 模式：expire_overdue_breaks 的 on_timeout 回调。
    单独命名而不是 lambda，便于 traceback 定位 + smoke 测试通过 import 校验。"""
    # BUG-010 · 系统刚把 AFK 员工强制回座为 working，登记抑制窗，避免紧随的 idle
    # 上报（员工还没真回座）立刻触发 idle_enter，与本条 Break Timeout 撞同一分钟。
    auto_return.mark(employee_id)
    notifier.notify_break_timeout(
        employee_id=employee_id,
        employee_name=employee_name,
        break_type=break_type,
        duration_seconds=duration_seconds,
        auto_returned=True,
    )


def _on_break_timeout_no_return(employee_id: int, employee_name: str,
                                 break_type: str, duration_seconds: int) -> None:
    """GS-13 · 自动回座 OFF 模式：保持 break 状态，仅提醒员工手动返回。"""
    notifier.notify_break_timeout(
        employee_id=employee_id,
        employee_name=employee_name,
        break_type=break_type,
        duration_seconds=duration_seconds,
        auto_returned=False,
    )


def _sweep_once(db) -> None:
    """单次清扫。GS-13：按 auto_return_after_break_timeout 走两条路径。

    ON（默认）：expire_overdue_breaks 自动结束 break + 恢复 working + 发"已自动回座"通知。
    OFF：find_overdue_breaks 只读取超时 break，不改状态，对**新出现**的超时 break
         发一次"请手动返回"通知（去重 set 防每 tick 重发）。
    """
    try:
        auto_return = bool(settings_service.get("auto_return_after_break_timeout"))
    except Exception:  # noqa: BLE001  读设置失败按默认 ON 处理（更安全：仍会自动回座）
        logger.exception("读取 auto_return_after_break_timeout 失败，按 ON 处理")
        auto_return = True

    if auto_return:
        # Phase 6.5A · on_timeout 触发 GS-3 通知；notifier 内部判 enabled+telegram。
        results = work_state.expire_overdue_breaks(db, on_timeout=_on_break_timeout)
        if results:
            logger.info("sweeper 自动结束 %d 个超时 break", len(results))
        return

    # OFF 模式：只通知不回座
    overdue = work_state.find_overdue_breaks(db)
    open_ids = {o.break_id for o in overdue}
    # 剔除已不再 open 的旧 id（员工已手动回座/下班）
    _off_mode_notified.intersection_update(open_ids)
    fired = 0
    for o in overdue:
        if o.break_id in _off_mode_notified:
            continue  # 已通知过，等员工手动返回，不重复刷屏
        _off_mode_notified.add(o.break_id)
        _on_break_timeout_no_return(
            o.employee_id, o.employee_name, o.break_type, o.duration_seconds
        )
        fired += 1
    if fired:
        logger.info("sweeper(OFF) 对 %d 个超时 break 发送手动返回提醒（保持休息状态）", fired)


class BreakSweeper:
    def __init__(self) -> None:
        self._thread: threading.Thread | None = None
        self._stop = threading.Event()

    def start(self) -> None:
        if self._thread and self._thread.is_alive():
            return
        self._stop.clear()
        self._thread = threading.Thread(
            target=self._run, name="break-sweeper", daemon=True
        )
        self._thread.start()
        logger.info(
            "break sweeper 已启动，每 %s 秒清扫一次", settings.break_sweep_interval_seconds
        )

    def stop(self) -> None:
        self._stop.set()
        if self._thread:
            self._thread.join(timeout=10)
        logger.info("break sweeper 已停止")

    def _run(self) -> None:
        # 先等一个周期再扫，避免启动瞬间与建表/其它启动逻辑争用
        while not self._stop.wait(settings.break_sweep_interval_seconds):
            try:
                db = SessionLocal()
                try:
                    # GS-13 · 按 auto_return_after_break_timeout 走 ON/OFF 两条路径。
                    _sweep_once(db)
                finally:
                    db.close()
            except Exception:  # noqa: BLE001  清扫失败不崩，下个周期再试
                logger.exception("break sweeper 执行失败")


break_sweeper = BreakSweeper()

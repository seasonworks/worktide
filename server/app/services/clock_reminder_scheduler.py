"""Phase 6.5A Sprint D · Clock In / Out 提醒守护线程。

设计要点（仿 sweeper.py 的 Event-wait 模式）：

- 每 `clock_reminder_check_interval_seconds` (默认 60s) tick 一次
- 每次 tick：取当前 UTC，按 `reminders_timezone_offset_hours`（默认 +8）换算成本地
  时间；比较 HH:MM 是否等于配置的 `clock_in_reminder_time` / `clock_out_reminder_time`
- 命中分钟内可能多次 tick（如果 interval 缩短），用 "最近一次 fire 的日期 (YYYY-MM-DD)"
  字典做幂等去重 — 当天只发一次
- 目标员工筛选（review 锁定）：
    Clock-In : work_state == "off_shift"            该上班却没打卡
    Clock-Out: work_state in {working, break_*}     该下班却还在岗
- `is_active` 必须为 True
- enabled 开关 + Telegram env 配置由 notifier 内部自查；scheduler 这里**先查 enabled**
  以避免无谓地查 DB（提早返回）
- 线程异常吃掉 + 下个 tick 继续，与 sweeper 同范式
"""
from __future__ import annotations

import logging
import threading
from datetime import datetime, timedelta, timezone

from sqlalchemy import select
from sqlalchemy.orm import Session

from ..config import settings
from ..constants import WorkState
from ..database import SessionLocal
from ..models import Employee, EmployeeStatusRow
from ..notifier import notifier
from .settings_service import settings_service

logger = logging.getLogger(__name__)


# work_state 值集合：在岗（含 break）
_ON_SHIFT_STATES = frozenset([
    WorkState.working.value,
    WorkState.break_meal.value,
    WorkState.break_toilet.value,
    WorkState.break_smoke.value,
])


def _now_local() -> datetime:
    """取当前 UTC 时间加 settings 配的偏移得到本地时间。

    抽成函数便于 smoke 注入。time.sleep 不影响这里，因为每次 tick 都重新算。
    """
    offset_hours = int(settings_service.get("reminders_timezone_offset_hours"))
    return datetime.now(timezone.utc) + timedelta(hours=offset_hours)


def _shift_hhmm(hhmm: str, delta_minutes: int) -> str:
    """把 "HH:MM" 平移 delta_minutes 分钟（可负），按 24h 取模。

    GS-11 group 模式上班提醒 = clock_in_reminder_time - pre_clock_in_minutes。
    例：09:00 - 15 → 08:45；00:05 - 15 → 23:50。
    """
    h, m = hhmm.split(":")
    total = (int(h) * 60 + int(m) + delta_minutes) % (24 * 60)
    return f"{total // 60:02d}:{total % 60:02d}"


def _select_clock_in_targets(db: Session) -> list[tuple[int, str]]:
    """需要"上班提醒"的员工：active 且 work_state == off_shift。

    返回 (employee_id, employee_name) 列表。EmployeeStatusRow 不存在时默认 off_shift。
    """
    emps = db.scalars(
        select(Employee).where(Employee.is_active.is_(True))
    ).all()
    status_map = {s.employee_id: s.work_state
                  for s in db.scalars(select(EmployeeStatusRow)).all()}
    targets: list[tuple[int, str]] = []
    for emp in emps:
        ws = status_map.get(emp.id, WorkState.off_shift.value)
        if ws == WorkState.off_shift.value:
            targets.append((emp.id, emp.name))
    return targets


def _select_clock_out_targets(db: Session) -> list[tuple[int, str]]:
    """需要"下班提醒"的员工：active 且 work_state 在 working / break_* 集合。

    返回 (employee_id, employee_name) 列表。
    """
    emps = db.scalars(
        select(Employee).where(Employee.is_active.is_(True))
    ).all()
    status_map = {s.employee_id: s.work_state
                  for s in db.scalars(select(EmployeeStatusRow)).all()}
    targets: list[tuple[int, str]] = []
    for emp in emps:
        ws = status_map.get(emp.id, WorkState.off_shift.value)
        if ws in _ON_SHIFT_STATES:
            targets.append((emp.id, emp.name))
    return targets


class ClockReminderScheduler:
    """单线程定时器：上下班时刻到点扫一遍员工，给目标群发提醒。"""

    def __init__(self) -> None:
        self._thread: threading.Thread | None = None
        self._stop = threading.Event()
        # "clock_in" / "clock_out" -> 当天 fired 的 "YYYY-MM-DD"（本地日期）
        # 每天最多发一次，幂等去重
        self._last_fired_date: dict[str, str] = {}

    # 暴露给 smoke：直接驱动一次判定，不走线程
    def tick(self, now_local: datetime | None = None) -> dict:
        """单步驱动 + 返回本 tick 处理结果（供 smoke / debug）。

        返回 dict：
            {"checked_at_local": "...", "clock_in_fired_for": [...], "clock_out_fired_for": [...]}
        """
        result = {"checked_at_local": None,
                  "clock_in_fired_for": [],
                  "clock_out_fired_for": []}
        try:
            local = now_local if now_local is not None else _now_local()
            result["checked_at_local"] = local.strftime("%Y-%m-%d %H:%M:%S")
            db = SessionLocal()
            try:
                self._maybe_fire(db, local, kind="clock_in", result=result)
                self._maybe_fire(db, local, kind="clock_out", result=result)
            finally:
                db.close()
        except Exception:  # noqa: BLE001
            logger.exception("clock_reminder tick 执行失败")
        return result

    def _maybe_fire(self, db: Session, local: datetime, kind: str, result: dict) -> None:
        if kind == "clock_in":
            enabled_key = "notify_clock_in_reminder_enabled"
            time_key = "clock_in_reminder_time"
            selector = _select_clock_in_targets
            sender = notifier.notify_clock_in_reminder
            fired_field = "clock_in_fired_for"
        elif kind == "clock_out":
            enabled_key = "notify_clock_out_reminder_enabled"
            time_key = "clock_out_reminder_time"
            selector = _select_clock_out_targets
            sender = notifier.notify_clock_out_reminder
            fired_field = "clock_out_fired_for"
        else:
            return

        if not bool(settings_service.get(enabled_key)):
            return  # 子开关 OFF

        mode = str(settings_service.get("clock_reminder_mode"))
        base_hhmm = str(settings_service.get(time_key))
        # GS-11 group：上班提醒提前 pre_clock_in_minutes（下班准点）；individual：原时刻
        if mode == "group" and kind == "clock_in":
            pre = int(settings_service.get("pre_clock_in_minutes"))
            target_hhmm = _shift_hhmm(base_hhmm, -pre)
        else:
            target_hhmm = base_hhmm

        if local.strftime("%H:%M") != target_hhmm:
            return  # 不在（计算后的）配置时刻

        today_local = local.strftime("%Y-%m-%d")
        if self._last_fired_date.get(kind) == today_local:
            return  # 今日已发，幂等（按 (kind, 日期) 去重）

        if mode == "group":
            # GS-11 · 群发一次，不按员工逐个
            try:
                if kind == "clock_in":
                    pre = int(settings_service.get("pre_clock_in_minutes"))
                    notifier.notify_clock_in_group_reminder(pre_minutes=pre)
                else:
                    notifier.notify_clock_out_group_reminder()
                result[fired_field].append({"group": True})
                logger.info("clock_reminder · %s · 群发一次 @ %s（group 模式）",
                            kind, target_hhmm)
            except Exception:  # noqa: BLE001
                logger.exception("clock_reminder 群发失败 kind=%s", kind)
            self._last_fired_date[kind] = today_local
            return

        # individual（兼容）：按员工逐个
        targets = selector(db)
        logger.info("clock_reminder · %s · 命中 %d 名目标员工 @ %s（individual 模式）",
                    kind, len(targets), target_hhmm)
        for emp_id, name in targets:
            try:
                # notifier 内部已 gate enabled / cooldown，这里只负责把人喂进去
                sender(employee_id=emp_id, employee_name=name)
                result[fired_field].append({"employee_id": emp_id, "name": name})
            except Exception:  # noqa: BLE001
                logger.exception("clock_reminder 发送失败 emp=%s kind=%s", emp_id, kind)
        self._last_fired_date[kind] = today_local

    # 供 smoke / 测试 reset
    def reset_state(self) -> None:
        self._last_fired_date.clear()

    # 线程生命周期
    def start(self) -> None:
        if self._thread and self._thread.is_alive():
            return
        self._stop.clear()
        self._thread = threading.Thread(
            target=self._run, name="clock-reminder", daemon=True
        )
        self._thread.start()
        logger.info(
            "clock-reminder scheduler 已启动，每 %s 秒检查一次",
            settings.clock_reminder_check_interval_seconds,
        )

    def stop(self) -> None:
        self._stop.set()
        if self._thread:
            self._thread.join(timeout=10)
        logger.info("clock-reminder scheduler 已停止")

    def _run(self) -> None:
        # 首个周期前 wait，避开启动期与建表/migration 争用
        while not self._stop.wait(settings.clock_reminder_check_interval_seconds):
            self.tick()


# 进程单例（与 break_sweeper / cleanup_service 同模式）
clock_reminder_scheduler = ClockReminderScheduler()

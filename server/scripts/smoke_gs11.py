"""Phase 6.5B · GS-11 Clock Reminder 群发优化 冒烟（**dry-run / 0 真发**）。

    server/.venv/Scripts/python.exe server/scripts/smoke_gs11.py

覆盖：
    1  _shift_hhmm 平移（含跨午夜）
    2  模板：群发上班/下班文案
    3  group 模式 · 上班提醒在 (clock_in_time - pre) 群发一次（不按员工）
    4  group 模式 · 下班提醒在 clock_out_time 群发一次
    5  group 模式 · 每天只发一次（(kind, date) 幂等）；非目标时刻不发
    6  individual 模式 · 按员工逐个发（兼容保留）
"""
from __future__ import annotations

import os
import sys
from datetime import datetime
from pathlib import Path

if hasattr(sys.stdout, "reconfigure"):
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")

os.environ.setdefault("DATABASE_URL", "sqlite:///:memory:")
os.environ.setdefault("CORS_ORIGINS", "[]")
os.environ.setdefault("TELEGRAM_ENABLED", "true")
os.environ.setdefault("TELEGRAM_BOT_TOKEN", "dummy:test")
os.environ.setdefault("TELEGRAM_CHAT_ID", "-100123")

_HERE = Path(__file__).resolve()
_SERVER = _HERE.parents[1]
if str(_SERVER) not in sys.path:
    sys.path.insert(0, str(_SERVER))

from sqlalchemy import create_engine  # noqa: E402
from sqlalchemy.orm import sessionmaker  # noqa: E402
from app.database import Base  # noqa: E402
import app.models as models  # noqa: E402

test_engine = create_engine("sqlite:///:memory:", future=True)
Base.metadata.create_all(test_engine)
TestSession = sessionmaker(bind=test_engine, expire_on_commit=False, future=True)

import app.database as db_mod  # noqa: E402
import app.services.settings_service as ss_mod  # noqa: E402
import app.services.clock_reminder_scheduler as crs_mod  # noqa: E402
db_mod.SessionLocal = TestSession  # type: ignore[assignment]
ss_mod.SessionLocal = TestSession  # type: ignore[assignment]
crs_mod.SessionLocal = TestSession  # type: ignore[assignment]

from app.notifier import (  # noqa: E402
    notifier, build_clock_in_group_reminder, build_clock_out_group_reminder,
)
from app.services.clock_reminder_scheduler import (  # noqa: E402
    ClockReminderScheduler, _shift_hhmm,
)
from app.constants import WorkState  # noqa: E402

# 可控 settings
_cfg = {
    "clock_reminder_mode": "group",
    "pre_clock_in_minutes": 15,
    "clock_in_reminder_time": "09:00",
    "clock_out_reminder_time": "18:00",
    "notify_clock_in_reminder_enabled": True,
    "notify_clock_out_reminder_enabled": True,
    "reminders_timezone_offset_hours": 8,
}
_orig_get = ss_mod.settings_service.get


def _fake_get(key):
    if key in _cfg:
        return _cfg[key]
    return _orig_get(key)


ss_mod.settings_service.get = _fake_get

# 捕获 notifier 调用（不真发）
_calls: list[str] = []
notifier.notify_clock_in_group_reminder = lambda pre_minutes, dry_run=False: _calls.append(f"group_in:{pre_minutes}")  # type: ignore
notifier.notify_clock_out_group_reminder = lambda dry_run=False: _calls.append("group_out")  # type: ignore
notifier.notify_clock_in_reminder = lambda employee_id, employee_name, dry_run=False: _calls.append(f"indiv_in:{employee_id}")  # type: ignore
notifier.notify_clock_out_reminder = lambda employee_id, employee_name, dry_run=False: _calls.append(f"indiv_out:{employee_id}")  # type: ignore

PASS = 0
FAIL: list[str] = []


def ok(label: str, cond: bool, detail: str = "") -> None:
    global PASS
    if cond:
        PASS += 1
        print(f"  ✅ {label}")
    else:
        FAIL.append(label)
        print(f"  ❌ {label}   {detail}")


def _local(hhmm: str) -> datetime:
    h, m = hhmm.split(":")
    return datetime(2026, 6, 4, int(h), int(m), 0)


print("[1] _shift_hhmm 平移")
ok("09:00 -15 = 08:45", _shift_hhmm("09:00", -15) == "08:45")
ok("00:05 -15 = 23:50（跨午夜）", _shift_hhmm("00:05", -15) == "23:50")
ok("23:50 +20 = 00:10（跨午夜）", _shift_hhmm("23:50", 20) == "00:10")
ok("18:00 -0 = 18:00", _shift_hhmm("18:00", 0) == "18:00")

print("[2] 群发模板")
t_in = build_clock_in_group_reminder(15)
ok("上班模板含 '上班提醒'", "上班提醒" in t_in)
ok("上班模板含 '还有 15 分钟'", "还有 15 分钟" in t_in, t_in)
ok("上班模板含 Start Work Reminder", "Start Work Reminder" in t_in)
t_out = build_clock_out_group_reminder()
ok("下班模板含 '下班提醒'", "下班提醒" in t_out)
ok("下班模板含 '已到下班时间'", "已到下班时间" in t_out)

print("[3] group · 上班提醒在 09:00-15=08:45 群发一次（不按员工）")
sch = ClockReminderScheduler()
_calls.clear()
r = sch.tick(now_local=_local("08:45"))
ok("08:45 触发 group_in:15", _calls == ["group_in:15"], str(_calls))
ok("result 标记 group", r["clock_in_fired_for"] == [{"group": True}], str(r["clock_in_fired_for"]))

print("[4] group · 下班提醒在 18:00 群发一次")
sch2 = ClockReminderScheduler()
_calls.clear()
sch2.tick(now_local=_local("18:00"))
ok("18:00 触发 group_out", _calls == ["group_out"], str(_calls))

print("[5] group · 每天只发一次 + 非目标时刻不发")
sch3 = ClockReminderScheduler()
_calls.clear()
sch3.tick(now_local=_local("08:45"))   # 第1次
sch3.tick(now_local=_local("08:45"))   # 同日同时刻再 tick
ok("同日只发一次（共1条）", _calls == ["group_in:15"], str(_calls))
_calls.clear()
sch3.tick(now_local=_local("10:00"))   # 非目标时刻
ok("非目标时刻不发", _calls == [], str(_calls))
# 09:00（individual 才在此刻发；group 应不发，因为 group 上班时刻=08:45）
_calls.clear()
sch4 = ClockReminderScheduler()
sch4.tick(now_local=_local("09:00"))
ok("group 模式 09:00 不发（提前到 08:45 了）", _calls == [], str(_calls))

print("[6] individual 模式 · 按员工逐个")
_cfg["clock_reminder_mode"] = "individual"
with TestSession() as db:
    for i in (201, 202):
        db.add(models.Employee(id=i, machine_id=f"m{i}", name=f"E{i}",
                               hostname=f"h{i}", status="online", idle_seconds=0, is_active=True))
        db.commit()
        db.add(models.EmployeeStatusRow(employee_id=i, work_state=WorkState.off_shift.value,
                                        active_break_id=None))
        db.commit()
sch5 = ClockReminderScheduler()
_calls.clear()
sch5.tick(now_local=_local("09:00"))   # individual 上班在 09:00（不提前）
ok("individual 09:00 逐个发（2 人）",
   sorted(_calls) == ["indiv_in:201", "indiv_in:202"], str(_calls))
ok("individual 不发 group", "group_in:15" not in _calls)

print()
print("=" * 60)
print(f"PASS={PASS}  FAIL={len(FAIL)}")
if FAIL:
    for label in FAIL:
        print(f"  - {label}")
    sys.exit(1)
print("GS-11 全部通过（0 真发）")

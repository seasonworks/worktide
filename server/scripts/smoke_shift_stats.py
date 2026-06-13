"""当前班次统计冒烟（work_state.shift_stats：迟到 / 累计挂机 / 当前vs最近班次 / break）。

    server/.venv/Scripts/python.exe server/scripts/smoke_shift_stats.py

覆盖：
    1  无任何班次 → None
    2  进行中班次：is_current=True、ended=None、gross≈窗口、break=0、net=gross、idle=0
    3  累计挂机：4×30s idle 段 → 120s（单段<封顶）
    4  挂机封顶：300s 大间隔单段 → 计 90s（封顶）
    5  含 break：meal_count=1、break_seconds>0、net=gross-break
    6  迟到：started_at 本地 22:02 vs 上班时刻 22:00 → late_seconds=120
    7  早到/准时：started_at 本地 21:50 → late_seconds=0
    8  已下班：返回最近一次已结束班次，is_current=False、ended 非空
    9  检测前导期计入：首条 idle 的 idle_seconds(900) + 3×30s = 990
   10  前导期封顶不超过开班时长（防开班前空闲串入）
"""
from __future__ import annotations

import os
import sys
from datetime import timedelta
from pathlib import Path

if hasattr(sys.stdout, "reconfigure"):
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")

os.environ.setdefault("DATABASE_URL", "sqlite:///:memory:")
os.environ.setdefault("CORS_ORIGINS", "[]")
os.environ.setdefault("TELEGRAM_ENABLED", "false")
os.environ.setdefault("TELEGRAM_BOT_TOKEN", "dummy:test")
os.environ.setdefault("TELEGRAM_CHAT_ID", "-100123")

_HERE = Path(__file__).resolve()
_SERVER = _HERE.parents[1]
if str(_SERVER) not in sys.path:
    sys.path.insert(0, str(_SERVER))

from datetime import datetime, timezone  # noqa: E402

from sqlalchemy import create_engine, select  # noqa: E402
from sqlalchemy.orm import sessionmaker  # noqa: E402

from app.database import Base  # noqa: E402
import app.models as models  # noqa: E402
import app.database as db_mod  # noqa: E402
import app.services.settings_service as ss_mod  # noqa: E402

test_engine = create_engine("sqlite:///:memory:", future=True)
Base.metadata.create_all(test_engine)
TestSession = sessionmaker(bind=test_engine, expire_on_commit=False, future=True)
db_mod.SessionLocal = TestSession  # type: ignore[assignment]
ss_mod.SessionLocal = TestSession  # type: ignore[assignment]

# 覆盖上班时刻/时区，其它键回退原 get
_orig_get = ss_mod.settings_service.get


def _fake_get(key):
    if key == "clock_in_reminder_time":
        return "22:00"
    if key == "reminders_timezone_offset_hours":
        return 7
    return _orig_get(key)


ss_mod.settings_service.get = _fake_get

from app.constants import WorkState  # noqa: E402
from app.services import work_state as ws  # noqa: E402

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


def _mk_emp(db, name: str) -> int:
    emp = models.Employee(
        machine_id=f"m_{name}", name=name, hostname=f"h_{name}",
        status="online", idle_seconds=0, is_active=True,
    )
    db.add(emp)
    db.commit()
    db.refresh(emp)
    return emp.id


def _open_shift(db, emp_id):
    return db.scalars(
        select(models.ShiftSession)
        .where(models.ShiftSession.employee_id == emp_id, models.ShiftSession.ended_at.is_(None))
    ).first()


def _log(db, emp_id, status, when, idle_seconds=0):
    db.add(models.ActivityLog(
        employee_id=emp_id, status=status, is_active=(status != "idle"),
        idle_seconds=idle_seconds, reported_at=when,
    ))


print("[1] 无任何班次 → None")
with TestSession() as db:
    eid = _mk_emp(db, "E_noshift")
    ok("None", ws.shift_stats(db, eid) is None)

print("[2] 进行中班次基础")
with TestSession() as db:
    eid = _mk_emp(db, "E_cur")
    ws.clock_in(db, eid)
    sh = _open_shift(db, eid)
    sh.started_at = models.utcnow() - timedelta(seconds=600)  # 10 分钟前
    db.commit()
    s = ws.shift_stats(db, eid)
    ok("is_current=True", s["is_current"] is True)
    ok("ended=None", s["shift_ended_at"] is None)
    ok("gross≈600", 590 <= s["gross_shift_seconds"] <= 630, str(s["gross_shift_seconds"]))
    ok("break=0", s["break_seconds"] == 0)
    ok("net=gross", s["net_work_seconds"] == s["gross_shift_seconds"])
    ok("idle=0（无日志）", s["idle_seconds_total"] == 0, str(s["idle_seconds_total"]))

print("[3] 累计挂机 4×30s → 120s")
with TestSession() as db:
    eid = _mk_emp(db, "E_idle")
    ws.clock_in(db, eid)
    sh = _open_shift(db, eid)
    t0 = models.utcnow() - timedelta(seconds=600)
    sh.started_at = t0
    for k in range(4):
        _log(db, eid, "idle", t0 + timedelta(seconds=60 + 30 * k))
    _log(db, eid, "online", t0 + timedelta(seconds=60 + 30 * 4))  # 收尾，避免末段算到 now
    db.commit()
    s = ws.shift_stats(db, eid)
    ok("idle_total=120", s["idle_seconds_total"] == 120, str(s["idle_seconds_total"]))

print("[4] 挂机封顶：300s 大间隔单段 → 90s")
with TestSession() as db:
    eid = _mk_emp(db, "E_cap")
    ws.clock_in(db, eid)
    sh = _open_shift(db, eid)
    t0 = models.utcnow() - timedelta(seconds=600)
    sh.started_at = t0
    _log(db, eid, "idle", t0 + timedelta(seconds=60))
    _log(db, eid, "online", t0 + timedelta(seconds=360))  # 间隔 300s
    db.commit()
    s = ws.shift_stats(db, eid)
    ok("单段封顶 90s", s["idle_seconds_total"] == 90, str(s["idle_seconds_total"]))

print("[5] 含 break")
with TestSession() as db:
    eid = _mk_emp(db, "E_brk")
    ws.clock_in(db, eid)
    sh = _open_shift(db, eid)
    sh.started_at = models.utcnow() - timedelta(seconds=1200)
    db.commit()
    ws.start_break(db, eid, "meal")
    bk = db.scalars(
        select(models.BreakSession)
        .where(models.BreakSession.employee_id == eid, models.BreakSession.ended_at.is_(None))
    ).first()
    bk.started_at = models.utcnow() - timedelta(seconds=300)  # 进行中午餐 5 分钟
    db.commit()
    s = ws.shift_stats(db, eid)
    ok("meal_count=1", s["meal_count"] == 1, str(s["meal_count"]))
    ok("break_seconds>0", s["break_seconds"] > 0, str(s["break_seconds"]))
    ok("net=gross-break", s["net_work_seconds"] == max(0, s["gross_shift_seconds"] - s["break_seconds"]))

print("[6] 迟到：本地 22:02 vs 22:00 → 120s")
with TestSession() as db:
    eid = _mk_emp(db, "E_late")
    ws.clock_in(db, eid)
    sh = _open_shift(db, eid)
    sh.started_at = datetime(2026, 6, 10, 15, 2, 0, tzinfo=timezone.utc)  # +7 = 22:02
    db.commit()
    s = ws.shift_stats(db, eid)
    ok("late_seconds=120", s["late_seconds"] == 120, str(s["late_seconds"]))

print("[7] 早到：本地 21:50 → 0")
with TestSession() as db:
    eid = _mk_emp(db, "E_early")
    ws.clock_in(db, eid)
    sh = _open_shift(db, eid)
    sh.started_at = datetime(2026, 6, 10, 14, 50, 0, tzinfo=timezone.utc)  # +7 = 21:50
    db.commit()
    s = ws.shift_stats(db, eid)
    ok("late_seconds=0", s["late_seconds"] == 0, str(s["late_seconds"]))

print("[8] 已下班 → 最近一次已结束班次")
with TestSession() as db:
    eid = _mk_emp(db, "E_off")
    ws.clock_in(db, eid)
    ws.clock_out(db, eid)
    s = ws.shift_stats(db, eid)
    ok("非 None", s is not None)
    ok("is_current=False", s and s["is_current"] is False)
    ok("ended 非空", s and s["shift_ended_at"] is not None)

print("[9] 检测前导期计入：首条 idle 的 idle_seconds(900) + 3×30s 间隔 = 990")
with TestSession() as db:
    eid = _mk_emp(db, "E_lead")
    ws.clock_in(db, eid)
    sh = _open_shift(db, eid)
    t0 = models.utcnow() - timedelta(seconds=1200)
    sh.started_at = t0
    # 首条 idle 已空闲 900s（阈值前导期，之前都被记成 online）
    _log(db, eid, "idle",   t0 + timedelta(seconds=900), idle_seconds=900)
    _log(db, eid, "idle",   t0 + timedelta(seconds=930), idle_seconds=930)
    _log(db, eid, "idle",   t0 + timedelta(seconds=960), idle_seconds=960)
    _log(db, eid, "online", t0 + timedelta(seconds=990))
    db.commit()
    s = ws.shift_stats(db, eid)
    ok("idle_total=990（含 900 前导期）", s["idle_seconds_total"] == 990, str(s["idle_seconds_total"]))

print("[10] 前导期封顶不超过开班时长（防开班前空闲串入）")
with TestSession() as db:
    eid = _mk_emp(db, "E_guard")
    ws.clock_in(db, eid)
    sh = _open_shift(db, eid)
    t0 = models.utcnow() - timedelta(seconds=100)  # 刚开班 100s
    sh.started_at = t0
    # 首条 idle 报了超大 idle_seconds（机器开班前就空闲很久）→ 前导只能算到开班起点
    _log(db, eid, "idle",   t0 + timedelta(seconds=30), idle_seconds=5000)
    _log(db, eid, "online", t0 + timedelta(seconds=60))
    db.commit()
    s = ws.shift_stats(db, eid)
    # 前导 ≤ 30（开班到该刻），+30 间隔 = 60；绝不会是 5000+
    ok("前导不超开班时长（≈60，非 5000）", s["idle_seconds_total"] == 60, str(s["idle_seconds_total"]))

print()
print("=" * 60)
print(f"PASS={PASS}  FAIL={len(FAIL)}")
if FAIL:
    for label in FAIL:
        print(f"  - {label}")
    sys.exit(1)
print("当前班次统计全部通过")

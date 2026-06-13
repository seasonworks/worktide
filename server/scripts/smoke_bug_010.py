"""Phase 6.5A · BUG-010 修复冒烟（auto_return 抑制 idle_enter，**dry-run / 0 真发**）。

    server/.venv/Scripts/python.exe server/scripts/smoke_bug_010.py

覆盖：
    1  auto_return 模块边界：mark / is_suppressed / 120s 窗口 / reset
    2  activity：auto_return 后"立即"idle 上报 → idle_enter 被抑制
    3  activity：auto_return 后"60s"idle 上报 → 仍抑制（窗口内）
    4  activity：auto_return 后"超窗(121s)"idle 上报 → idle_enter 正常触发
    5  仅抑制 idle_enter：窗口内 idle→active 转换 → idle_exit 仍正常触发
    6  无 auto_return mark → idle_enter 正常触发（基线）
"""
from __future__ import annotations

import os
import sys
import urllib.request
from datetime import datetime, timedelta, timezone
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

# ---- 物理切断 Telegram 真发 ----
from app.notifier import TelegramNotifier, notifier  # noqa: E402

_sent: list[str] = []


def _fake_send(self, text: str, parse_mode=None, fallback_text=None) -> None:  # noqa: ANN001
    head = text.split("\n", 1)[0]
    if "🟡" in head:
        _sent.append("idle_enter")
    elif "🟢" in head:
        _sent.append("idle_exit")
    else:
        _sent.append("other")


TelegramNotifier._send = _fake_send
urllib.request.urlopen = lambda *a, **kw: (_ for _ in ()).throw(  # type: ignore[assignment]
    RuntimeError("smoke 严禁真发 Telegram")
)

# ---- in-memory DB ----
from sqlalchemy import create_engine  # noqa: E402
from sqlalchemy.orm import sessionmaker  # noqa: E402

from app.database import Base  # noqa: E402
import app.models as models  # noqa: E402

test_engine = create_engine("sqlite:///:memory:", future=True)
Base.metadata.create_all(test_engine)
TestSession = sessionmaker(bind=test_engine, expire_on_commit=False, future=True)

import app.database as db_mod  # noqa: E402
import app.services.settings_service as ss_mod  # noqa: E402
db_mod.SessionLocal = TestSession  # type: ignore[assignment]
ss_mod.SessionLocal = TestSession  # type: ignore[assignment]
import app.notifier as _nm  # noqa: E402
_nm.SessionLocal = TestSession  # type: ignore[assignment]  # 6.5B mention 解析走 in-memory

from app.constants import WorkState  # noqa: E402
from app.services import auto_return, manual_punch  # noqa: E402
from app.routers import activity as activity_mod  # noqa: E402
from app.schemas import ActivityReport  # noqa: E402
from fastapi import BackgroundTasks  # noqa: E402
import asyncio  # noqa: E402

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


def _mk_emp(name: str, work_state: str = WorkState.working.value) -> int:
    with TestSession() as db:
        emp = models.Employee(
            machine_id=f"m_{name}", name=name, hostname=f"h_{name}",
            status="online", idle_seconds=0, is_active=True,
        )
        db.add(emp)
        db.commit()
        db.refresh(emp)
        db.add(models.EmployeeStatusRow(employee_id=emp.id, work_state=work_state, active_break_id=None))
        db.commit()
        return emp.id


def _report(machine_id: str, idle_seconds: int) -> None:
    with TestSession() as db:
        bt = BackgroundTasks()
        activity_mod.report_activity(
            report=ActivityReport(machine_id=machine_id, hostname=f"h_{machine_id}", idle_seconds=idle_seconds),
            background_tasks=bt, db=db,
        )
        asyncio.get_event_loop().run_until_complete(bt())


def _fresh():
    activity_mod._reset_transition_state()
    manual_punch.reset()
    auto_return.reset()
    notifier.reset_cooldowns()
    _sent.clear()


print("[1] auto_return 模块边界（窗口 120s）")
auto_return.reset()
now = datetime.now(timezone.utc)
ok("初始未 mark → False", auto_return.is_suppressed(1, now) is False)
auto_return.mark(1, when=now)
ok("刚 mark → True", auto_return.is_suppressed(1, now) is True)
ok("+60s → True（窗内）", auto_return.is_suppressed(1, now + timedelta(seconds=60)) is True)
ok("+120s → True（边界含）", auto_return.is_suppressed(1, now + timedelta(seconds=120)) is True)
ok("+121s → False（超窗）", auto_return.is_suppressed(1, now + timedelta(seconds=121)) is False)
ok("其它员工不串扰", auto_return.is_suppressed(2, now) is False)
ok("SUPPRESS_SECONDS == 120", auto_return.SUPPRESS_SECONDS == 120)

print("[2] auto_return 后立即 idle 上报 → idle_enter 抑制")
_fresh()
eid = _mk_emp("E_immediate", WorkState.working.value)
auto_return.mark(eid, when=datetime.now(timezone.utc))  # 刚回座
_report("m_E_immediate", 1700)  # idle 跨阈值
ok("idle_enter 被抑制（0 条）", _sent.count("idle_enter") == 0, str(_sent))

print("[3] auto_return 后 60s idle 上报 → 仍抑制")
_fresh()
eid = _mk_emp("E_60s", WorkState.working.value)
auto_return.mark(eid, when=datetime.now(timezone.utc) - timedelta(seconds=60))
_report("m_E_60s", 1700)
ok("60s 内 idle_enter 仍抑制（0 条）", _sent.count("idle_enter") == 0, str(_sent))

print("[4] auto_return 后超窗(121s) idle 上报 → idle_enter 正常触发")
_fresh()
eid = _mk_emp("E_beyond", WorkState.working.value)
auto_return.mark(eid, when=datetime.now(timezone.utc) - timedelta(seconds=121))
_report("m_E_beyond", 1700)
ok("超窗 idle_enter 正常发（1 条）", _sent.count("idle_enter") == 1, str(_sent))

print("[5] 仅抑制 idle_enter：窗口内 idle→active → idle_exit 仍触发")
_fresh()
eid = _mk_emp("E_exit", WorkState.working.value)
auto_return.mark(eid, when=datetime.now(timezone.utc))  # 窗口内
_report("m_E_exit", 1700)   # → idle（idle_enter 应被抑制）
ok("窗口内 idle_enter 抑制", _sent.count("idle_enter") == 0, str(_sent))
_report("m_E_exit", 0)      # → active（idle_exit 不受 auto_return 影响）
ok("idle_exit 仍正常触发（1 条）", _sent.count("idle_exit") == 1, str(_sent))

print("[6] 无 auto_return mark → idle_enter 正常触发（基线）")
_fresh()
eid = _mk_emp("E_base", WorkState.working.value)
_report("m_E_base", 1700)
ok("无 mark idle_enter 正常发（1 条）", _sent.count("idle_enter") == 1, str(_sent))

print()
print("=" * 60)
print(f"PASS={PASS}  FAIL={len(FAIL)}")
if FAIL:
    for label in FAIL:
        print(f"  - {label}")
    sys.exit(1)
print("BUG-010 修复全部通过（仅抑制 idle_enter；0 真发）")

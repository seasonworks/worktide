"""挂机重复提醒冒烟（每跨一个 idle_threshold 段发一次：15/30/45 min…），dry-run / 0 真发。

    server/.venv/Scripts/python.exe server/scripts/smoke_idle_repeat.py

阈值默认 900s(15min)。覆盖：
    1  idle 1000s(段1) → 发 1 次，idle_seconds≈1000
    2  idle 1700s(仍段1) → 不重复发
    3  idle 1900s(段2) → 再发（idle_seconds≈1900）
    4  idle 2800s(段3) → 再发
    5  active(0) → 发 idle_exit + bucket 清空
    6  再 idle 1000s(段1) → 重新从段1发（退出后重计）
    7  off_shift 挂机 → 不发（不可通知）
    8  break 中挂机 → 不发（detection paused）
"""
from __future__ import annotations

import asyncio
import os
import sys
import urllib.request
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

urllib.request.urlopen = lambda *a, **kw: (_ for _ in ()).throw(  # type: ignore[assignment]
    RuntimeError("smoke 严禁真发 Telegram")
)

from sqlalchemy import create_engine  # noqa: E402
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

from app.constants import WorkState  # noqa: E402
from app.notifier import notifier  # noqa: E402
from app.routers import activity as activity_mod  # noqa: E402
from app.schemas import ActivityReport  # noqa: E402
from app.services import auto_return, manual_punch  # noqa: E402
from fastapi import BackgroundTasks  # noqa: E402

# 捕获 notify 调用（不走真模板/真发）
_enter: list[int] = []
_exit: list[int] = []


def _cap_enter(employee_id, employee_name, idle_seconds, dry_run=False):  # noqa: ANN001
    _enter.append(idle_seconds)


def _cap_exit(employee_id, employee_name, idle_duration_seconds, dry_run=False):  # noqa: ANN001
    _exit.append(idle_duration_seconds)


notifier.notify_idle_enter = _cap_enter  # type: ignore[assignment]
notifier.notify_idle_exit = _cap_exit    # type: ignore[assignment]

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


def _mk_emp(name: str, work_state: str = WorkState.working.value) -> None:
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


def _report(name: str, idle_seconds: int) -> None:
    with TestSession() as db:
        bt = BackgroundTasks()
        activity_mod.report_activity(
            report=ActivityReport(machine_id=f"m_{name}", hostname=f"h_{name}", idle_seconds=idle_seconds),
            background_tasks=bt, db=db,
        )
        asyncio.get_event_loop().run_until_complete(bt())


def _fresh():
    activity_mod._reset_transition_state()
    manual_punch.reset()
    auto_return.reset()
    notifier.reset_cooldowns()
    _enter.clear()
    _exit.clear()


print("[1-6] 连续挂机跨多个阈值段 + 退出 + 重新挂机（threshold=900）")
_fresh()
_mk_emp("E_rep", WorkState.working.value)
_report("E_rep", 1000)   # 段1 → 发
ok("段1 发 1 次", len(_enter) == 1, str(_enter))
_report("E_rep", 1700)   # 仍段1 → 不发
ok("仍段1 不重复", len(_enter) == 1, str(_enter))
_report("E_rep", 1900)   # 段2 → 发
ok("段2 再发（共2）", len(_enter) == 2, str(_enter))
ok("段2 的 idle_seconds≈1900", _enter[-1] == 1900, str(_enter))
_report("E_rep", 2800)   # 段3 → 发
ok("段3 再发（共3）", len(_enter) == 3, str(_enter))
_report("E_rep", 0)      # active → exit + 清 bucket
ok("退出发 idle_exit 1 次", len(_exit) == 1, str(_exit))
_report("E_rep", 1000)   # 重新挂机段1 → 再发
ok("退出后重新挂机段1 再发（共4）", len(_enter) == 4, str(_enter))

print("[7] off_shift 挂机 → 不发")
_fresh()
_mk_emp("E_off", WorkState.off_shift.value)
_report("E_off", 1900)   # 即便跨段也不发
ok("off_shift 不发挂机提醒", len(_enter) == 0, str(_enter))

print("[8] break 中挂机 → 不发（detection paused）")
_fresh()
_mk_emp("E_brk", WorkState.break_meal.value)
_report("E_brk", 1900)
ok("break 中不发挂机提醒", len(_enter) == 0, str(_enter))

print()
print("=" * 60)
print(f"PASS={PASS}  FAIL={len(FAIL)}")
if FAIL:
    for label in FAIL:
        print(f"  - {label}")
    sys.exit(1)
print("挂机重复提醒全部通过（每阈值段一次；0 真发）")

"""进程重启后状态回填冒烟（#2：重启不再重复 @ 挂机），dry-run / 0 真发。

    server/.venv/Scripts/python.exe server/scripts/smoke_restart_seed.py

阈值默认 900s(15min)。模拟"重启"= activity_mod._reset_transition_state()（清进程内
转换状态，DB 保留）。覆盖：
    1  重启前已挂机(段1)→重启→仍挂机(段1)：不重发 enter（回填 _prev_status + bucket）
    2  接上：跨到段2 → 正常发（重启后照常按阈值段提醒）
    3  重启前 online → 重启 → 现在 idle：正常发 enter（真·进入挂机不被抑制）
    4  重启前 idle → 重启 → 现在 online：不发 idle_exit（跨重启无法得知真实起点，宁缺毋错）
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


def _restart() -> None:
    """模拟进程重启：只清进程内转换状态，DB 保留。"""
    activity_mod._reset_transition_state()


def _fresh():
    activity_mod._reset_transition_state()
    manual_punch.reset()
    auto_return.reset()
    notifier.reset_cooldowns()
    _enter.clear()
    _exit.clear()


print("[1-2] 重启前已挂机 → 重启 → 仍挂机：不重发；跨段才发")
_fresh()
_mk_emp("R_idle", WorkState.working.value)
_report("R_idle", 1000)            # 重启前：段1 → 发
ok("重启前段1 发 1 次", len(_enter) == 1, str(_enter))
_restart()                          # ← 进程重启（清内存状态）
_report("R_idle", 1100)            # 重启后仍段1 → 回填后不重发
ok("重启后仍段1 不重发 enter", len(_enter) == 1, str(_enter))
_report("R_idle", 1900)            # 跨到段2 → 正常发
ok("重启后跨段2 正常发（共2）", len(_enter) == 2, str(_enter))

print("[3] 重启前 online → 重启 → 现在 idle：正常发 enter")
_fresh()
_mk_emp("R_on2idle", WorkState.working.value)
_report("R_on2idle", 10)           # 重启前：online（<阈值）
ok("重启前 online 不发", len(_enter) == 0, str(_enter))
_restart()
_report("R_on2idle", 1000)         # 重启后进入挂机 → 应发
ok("重启后真·进入挂机 正常发 enter", len(_enter) == 1, str(_enter))

print("[4] 重启前 idle → 重启 → 现在 online：不发 idle_exit（跨重启无真实起点）")
_fresh()
_mk_emp("R_idle2on", WorkState.working.value)
_report("R_idle2on", 1000)         # 重启前：挂机
ok("重启前挂机发 enter", len(_enter) == 1, str(_enter))
_restart()
_report("R_idle2on", 0)            # 重启后变 active
ok("重启后变 active 不发 idle_exit", len(_exit) == 0, str(_exit))

print()
print("=" * 60)
print(f"PASS={PASS}  FAIL={len(FAIL)}")
if FAIL:
    for label in FAIL:
        print(f"  - {label}")
    sys.exit(1)
print("重启状态回填全部通过（重启对挂机通知透明；0 真发）")

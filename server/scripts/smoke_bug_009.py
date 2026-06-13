"""Phase 6.5A · BUG-009 修复冒烟（off_shift 不参与 idle_enter / idle_exit）。

    server/.venv/Scripts/python.exe server/scripts/smoke_bug_009.py

覆盖：
    1. work_state.is_off_shift 返回值（off_shift True；其它 False）
    2. activity 路径：off_shift + idle 上报 → 不触发 notify_idle_enter
    3. activity 路径：working + idle 上报 → 触发 notify_idle_enter
    4. activity 路径：off_shift + idle→active 上报 → 不触发 notify_idle_exit
    5. activity 路径：working + idle→active → 触发 notify_idle_exit（正常）
    6. is_detection_paused 仍只看 break_*（语义未污染）
"""
from __future__ import annotations

import os
import sys
import urllib.request
from datetime import datetime, timezone
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

_sent_kinds: list[str] = []


def _fake_send(self, text: str, parse_mode=None, fallback_text=None) -> None:  # noqa: ANN001
    head = text.split("\n", 1)[0]
    if "🟡" in head:
        _sent_kinds.append("idle_enter")
    elif "🟢" in head:
        _sent_kinds.append("idle_exit")
    else:
        _sent_kinds.append("other")


TelegramNotifier._send = _fake_send
urllib.request.urlopen = lambda *a, **kw: (_ for _ in ()).throw(  # type: ignore[assignment]
    RuntimeError("smoke 严禁真发 Telegram")
)

# ---- in-memory DB ----
from sqlalchemy import create_engine  # noqa: E402
from sqlalchemy.orm import sessionmaker  # noqa: E402

from app.database import Base, SessionLocal  # noqa: E402
import app.models as models  # noqa: E402

test_engine = create_engine("sqlite:///:memory:", future=True)
Base.metadata.create_all(test_engine)
TestSession = sessionmaker(bind=test_engine, expire_on_commit=False, future=True)

# 把全局 SessionLocal 替换为测试 session（activity / work_state / settings 都用同一个）
import app.database as db_mod  # noqa: E402
import app.services.settings_service as ss_mod  # noqa: E402
import app.services.work_state as ws_mod  # noqa: E402
import app.routers.activity as activity_mod  # noqa: E402

db_mod.SessionLocal = TestSession  # type: ignore[assignment]
ss_mod.SessionLocal = TestSession  # type: ignore[assignment]
import app.notifier as _nm  # noqa: E402
_nm.SessionLocal = TestSession  # type: ignore[assignment]  # 6.5B mention 解析走 in-memory

from app.constants import WorkState  # noqa: E402
from app.services import manual_punch  # noqa: E402

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
            machine_id=f"m_{name}",
            name=name,
            hostname=f"host_{name}",
            status="online",
            idle_seconds=0,
            is_active=True,
        )
        db.add(emp)
        db.commit()
        db.refresh(emp)
        row = models.EmployeeStatusRow(
            employee_id=emp.id,
            work_state=work_state,
            active_break_id=None,
        )
        db.add(row)
        db.commit()
        return emp.id


print("[1] work_state.is_off_shift 返回值")
with TestSession() as db:
    e1 = _mk_emp("E_off", work_state=WorkState.off_shift.value)
    e2 = _mk_emp("E_work", work_state=WorkState.working.value)
    e3 = _mk_emp("E_meal", work_state=WorkState.break_meal.value)
    ok("off_shift → is_off_shift=True", ws_mod.is_off_shift(db, e1) is True)
    ok("working → is_off_shift=False", ws_mod.is_off_shift(db, e2) is False)
    ok("break_meal → is_off_shift=False", ws_mod.is_off_shift(db, e3) is False)
    ok("不存在的 emp → is_off_shift=False", ws_mod.is_off_shift(db, 99999) is False)

print()
print("[2] is_detection_paused 语义未污染（仍仅 break_*）")
with TestSession() as db:
    ok("off_shift → is_detection_paused=False",
       ws_mod.is_detection_paused(db, e1) is False)
    ok("working → is_detection_paused=False",
       ws_mod.is_detection_paused(db, e2) is False)
    ok("break_meal → is_detection_paused=True (因 break_pauses_detection 默认 True)",
       ws_mod.is_detection_paused(db, e3) is True)


# ---- activity 路径模拟 ----

from fastapi import BackgroundTasks  # noqa: E402

from app.schemas import ActivityReport  # noqa: E402


def _make_report(machine_id: str, idle_seconds: int) -> ActivityReport:
    return ActivityReport(
        machine_id=machine_id,
        hostname=f"host_{machine_id}",
        idle_seconds=idle_seconds,
    )


def _run_report(machine_id: str, idle_seconds: int) -> int:
    """直接调 activity.report_activity 返回 _sent_kinds 长度增量。"""
    activity_mod._reset_transition_state()
    manual_punch.reset()
    notifier.reset_cooldowns() if hasattr(notifier, "reset_cooldowns") else None
    _sent_kinds.clear()
    with TestSession() as db:
        bt = BackgroundTasks()
        activity_mod.report_activity(
            report=_make_report(machine_id, idle_seconds),
            background_tasks=bt,
            db=db,
        )
        # 把 BackgroundTasks 在主线程同步跑掉（FastAPI 在测试上下文不会执行）
        import asyncio
        asyncio.get_event_loop().run_until_complete(bt())
    return len(_sent_kinds)


print()
print("[3] off_shift + 单次 idle 上报 → 不触发 notify_idle_enter (BUG-009)")
_sent_kinds.clear()
activity_mod._reset_transition_state()
with TestSession() as db:
    bt = BackgroundTasks()
    # 阈值默认 900s；这里 idle_seconds=1700 → 服务端判 idle
    activity_mod.report_activity(_make_report("m_E_off", 1700), bt, db)
    import asyncio
    asyncio.get_event_loop().run_until_complete(bt())
ok("off_shift 员工 idle 上报后 _sent_kinds 为 0",
   len(_sent_kinds) == 0, detail=str(_sent_kinds))

print()
print("[4] working + 单次 idle 上报 → 触发 notify_idle_enter (基线)")
_sent_kinds.clear()
activity_mod._reset_transition_state()
manual_punch.reset()
notifier.reset_cooldowns() if hasattr(notifier, "reset_cooldowns") else None
with TestSession() as db:
    bt = BackgroundTasks()
    activity_mod.report_activity(_make_report("m_E_work", 1700), bt, db)
    import asyncio
    asyncio.get_event_loop().run_until_complete(bt())
ok("working 员工 idle 上报触发 1 次 idle_enter",
   _sent_kinds == ["idle_enter"], detail=str(_sent_kinds))

print()
print("[5] off_shift idle → off_shift active 转换 → 不触发 notify_idle_exit")
_sent_kinds.clear()
activity_mod._reset_transition_state()
manual_punch.reset()
notifier.reset_cooldowns() if hasattr(notifier, "reset_cooldowns") else None
with TestSession() as db:
    bt = BackgroundTasks()
    activity_mod.report_activity(_make_report("m_E_off", 1700), bt, db)
    import asyncio
    asyncio.get_event_loop().run_until_complete(bt())
# 现在 prev_status[e1] = idle；再发一次 active 上报
with TestSession() as db:
    bt = BackgroundTasks()
    activity_mod.report_activity(_make_report("m_E_off", 0), bt, db)
    asyncio.get_event_loop().run_until_complete(bt())
ok("off_shift idle→active 也不触发 idle_exit",
   _sent_kinds == [], detail=str(_sent_kinds))

print()
print("[6] working idle → working active 转换 → 触发 notify_idle_exit (基线)")
_sent_kinds.clear()
activity_mod._reset_transition_state()
manual_punch.reset()
notifier.reset_cooldowns() if hasattr(notifier, "reset_cooldowns") else None
# 用无历史的新员工：避免 #2 首见回填从 DB 查到旧 idle 日志而正确地不重发 enter
# （E_work 在 [4] 已留 idle 日志，整轮内存库持久 → 那是"重启前已挂机"语义，另测）
import asyncio  # noqa: E402
_mk_emp("E_work2", work_state=WorkState.working.value)
with TestSession() as db:
    bt = BackgroundTasks()
    activity_mod.report_activity(_make_report("m_E_work2", 1700), bt, db)
    asyncio.get_event_loop().run_until_complete(bt())
# 再发一次 active 上报：prev=idle→curr=online → idle_exit
with TestSession() as db:
    bt = BackgroundTasks()
    activity_mod.report_activity(_make_report("m_E_work2", 0), bt, db)
    asyncio.get_event_loop().run_until_complete(bt())
ok("working idle→active 触发 idle_enter + idle_exit",
   _sent_kinds == ["idle_enter", "idle_exit"], detail=str(_sent_kinds))


print()
print("=" * 60)
print(f"PASS={PASS}  FAIL={len(FAIL)}")
if FAIL:
    for label in FAIL:
        print(f"  - {label}")
    print(f"\n严禁真发：urlopen 拦截 OK；_sent_kinds 最终残留 {len(_sent_kinds)} 条")
    sys.exit(1)
print(f"\n严禁真发：urlopen 拦截 OK；本轮共拦截 _send {len(_sent_kinds)} 次（计数已在每节末清零，0 真发）")

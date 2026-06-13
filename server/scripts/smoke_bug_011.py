"""Bug-011 修复冒烟（/windows/report 幂等落库，不会因重复 client_event_id 500）。

    server/.venv/Scripts/python.exe server/scripts/smoke_bug_011.py

覆盖：
    1  批内重复 id：同一 payload 两条相同 client_event_id → accepted=1 deduped=1，DB 仅 1 行
    2  串行重传：同批发两次 → 第二次 accepted=0 deduped=N，无 500，无重复行
    3  竞态安全网：直接执行 ON CONFLICT DO NOTHING 语句（含已存在 id）→ 不抛 IntegrityError，旧行保留、新行入库
    4  混合批：新+旧混合 → accepted/deduped 拆分正确
    5  聚合仍触发：accepted>0 后 window_sessions 产出该员工的会话
    6  ended_at < started_at → 422 整批拒
    7  machine_id 未注册 → 400
    8  空 events → accepted=0 deduped=0
"""
from __future__ import annotations

import os
import sys
from datetime import datetime, timedelta, timezone
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

from fastapi import HTTPException  # noqa: E402
from sqlalchemy import create_engine, func, select  # noqa: E402
from sqlalchemy.dialects.sqlite import insert as sqlite_insert  # noqa: E402
from sqlalchemy.orm import sessionmaker  # noqa: E402

from app.database import Base  # noqa: E402
import app.models as models  # noqa: E402
import app.database as db_mod  # noqa: E402

test_engine = create_engine("sqlite:///:memory:", future=True)
Base.metadata.create_all(test_engine)
TestSession = sessionmaker(bind=test_engine, expire_on_commit=False, future=True)
db_mod.SessionLocal = TestSession  # type: ignore[assignment]

from app.models import WindowEventRaw, WindowSession  # noqa: E402
from app.routers import windows as windows_mod  # noqa: E402
from app.schemas import WindowEventIn, WindowReportIn  # noqa: E402

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


_BASE = datetime(2026, 6, 10, 8, 0, 0, tzinfo=timezone.utc)


def _mk_emp(machine_id: str, name: str) -> int:
    with TestSession() as db:
        emp = models.Employee(
            machine_id=machine_id, name=name, hostname=f"h_{name}",
            status="online", idle_seconds=0, is_active=True,
        )
        db.add(emp)
        db.commit()
        db.refresh(emp)
        return emp.id


def _ev(cid: str, offset_min: int = 0, dur: int = 60, proc: str = "chrome.exe") -> WindowEventIn:
    start = _BASE + timedelta(minutes=offset_min)
    return WindowEventIn(
        client_event_id=cid,
        process_name=proc,
        window_title="t",
        started_at=start,
        ended_at=start + timedelta(seconds=dur),
        duration_seconds=dur,
        had_input=True,
    )


def _report(machine_id: str, events: list[WindowEventIn]):
    with TestSession() as db:
        return windows_mod.report_windows(
            WindowReportIn(machine_id=machine_id, events=events), db=db
        )


def _raw_count(machine_id: str | None = None) -> int:
    with TestSession() as db:
        return db.scalar(select(func.count()).select_from(WindowEventRaw))


print("[1] 批内重复 id")
_mk_emp("m1", "E1")
out = _report("m1", [_ev("dup-1", 0), _ev("dup-1", 1)])  # 同 id 两条
ok("accepted=1", out.accepted == 1, str(out))
ok("deduped=1", out.deduped == 1, str(out))
ok("DB 仅 1 行", _raw_count() == 1, str(_raw_count()))

print("[2] 串行重传同批两次")
_mk_emp("m2", "E2")
o1 = _report("m2", [_ev("a", 0), _ev("b", 1)])
before = _raw_count()
o2 = _report("m2", [_ev("a", 0), _ev("b", 1)])  # 完全重传
ok("首次 accepted=2", o1.accepted == 2, str(o1))
ok("重传 accepted=0", o2.accepted == 0, str(o2))
ok("重传 deduped=2", o2.deduped == 2, str(o2))
ok("重传未新增行", _raw_count() == before, str(_raw_count()))

print("[3] 竞态安全网：ON CONFLICT DO NOTHING 不抛、跳过已存在")
with TestSession() as db:
    eid = db.scalar(select(models.Employee.id).where(models.Employee.machine_id == "m2"))
    # 模拟"预查漏掉、插入时已存在"：直接对含已存在 id 'a' + 新 id 'race-z' 的批跑 upsert
    stmt = (
        sqlite_insert(WindowEventRaw)
        .values([
            {"employee_id": eid, "client_event_id": "a", "process_name": "x",
             "window_title": "", "started_at": _BASE, "ended_at": _BASE + timedelta(seconds=1),
             "duration_seconds": 1, "had_input": True},
            {"employee_id": eid, "client_event_id": "race-z", "process_name": "x",
             "window_title": "", "started_at": _BASE, "ended_at": _BASE + timedelta(seconds=1),
             "duration_seconds": 1, "had_input": True},
        ])
        .on_conflict_do_nothing(index_elements=["client_event_id"])
    )
    raised = False
    try:
        res = db.execute(stmt)
        db.commit()
    except Exception as exc:  # noqa: BLE001
        raised = True
        print("   ", repr(exc))
    ok("含已存在 id 的批不抛 IntegrityError", raised is False)
    ok("rowcount=1（仅新行入库）", res.rowcount == 1, str(res.rowcount))
    cnt_z = db.scalar(select(func.count()).select_from(WindowEventRaw)
                      .where(WindowEventRaw.client_event_id == "race-z"))
    cnt_a = db.scalar(select(func.count()).select_from(WindowEventRaw)
                      .where(WindowEventRaw.client_event_id == "a"))
    ok("新行 race-z 入库", cnt_z == 1, str(cnt_z))
    ok("旧行 a 仍唯一未重复", cnt_a == 1, str(cnt_a))

print("[4] 混合批：新+旧拆分")
_mk_emp("m4", "E4")
_report("m4", [_ev("p", 0), _ev("q", 1)])
o = _report("m4", [_ev("q", 1), _ev("r", 2), _ev("s", 3)])  # q 旧, r/s 新
ok("accepted=2", o.accepted == 2, str(o))
ok("deduped=1", o.deduped == 1, str(o))

print("[5] 聚合仍触发：window_sessions 产出")
emp5 = _mk_emp("m5", "E5")
_report("m5", [_ev("agg-1", 0, dur=120), _ev("agg-2", 5, dur=120)])
with TestSession() as db:
    sess = db.scalar(select(func.count()).select_from(WindowSession)
                     .where(WindowSession.employee_id == emp5))
ok("该员工已生成 window_sessions", sess and sess > 0, f"sessions={sess}")

print("[6] ended_at < started_at → 422")
_mk_emp("m6", "E6")
bad = WindowEventIn(
    client_event_id="bad", process_name="x", window_title="",
    started_at=_BASE + timedelta(seconds=100), ended_at=_BASE,  # 倒序
    duration_seconds=0, had_input=True,
)
try:
    _report("m6", [bad])
    ok("422 抛出", False, "未抛")
except HTTPException as e:
    ok("422 抛出", e.status_code == 422, str(e.status_code))

print("[7] machine_id 未注册 → 400")
try:
    _report("no-such-machine", [_ev("x", 0)])
    ok("400 抛出", False, "未抛")
except HTTPException as e:
    ok("400 抛出", e.status_code == 400, str(e.status_code))

print("[8] 空 events")
_mk_emp("m8", "E8")
o = _report("m8", [])
ok("accepted=0 deduped=0", o.accepted == 0 and o.deduped == 0, str(o))

print()
print("=" * 60)
print(f"PASS={PASS}  FAIL={len(FAIL)}")
if FAIL:
    for label in FAIL:
        print(f"  - {label}")
    sys.exit(1)
print("Bug-011 修复全部通过（重复 client_event_id 不再 500；幂等落库）")

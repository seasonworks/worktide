"""Phase 6.5A · GS-13 Auto Return To Work Switch 冒烟（**dry-run / 0 真发**）。

    server/.venv/Scripts/python.exe server/scripts/smoke_gs13.py

覆盖：
    1  模板：auto_returned=True → "系统已自动返回工作岗位"
    2  模板：auto_returned=False → "休息时间已超过设定限制" + "请员工尽快返回工作岗位"
    3  find_overdue_breaks 只读：返回超时 break，但 ended_at 仍 None、work_state 不变
    4  sweeper ON 路径：_sweep_once 关闭超时 break + work_state→working + 发 auto_returned=True 通知
    5  sweeper OFF 路径：保持 break_* 状态 + 发 auto_returned=False 通知，且仅发一次（去重）
    6  OFF 去重：员工手动回座后 break_id 从去重 set 中剔除
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
os.environ.setdefault("TELEGRAM_ENABLED", "true")
os.environ.setdefault("TELEGRAM_BOT_TOKEN", "dummy:test")
os.environ.setdefault("TELEGRAM_CHAT_ID", "-100123")

_HERE = Path(__file__).resolve()
_SERVER = _HERE.parents[1]
if str(_SERVER) not in sys.path:
    sys.path.insert(0, str(_SERVER))

# ---- 物理切断 Telegram 真发 ----
import urllib.request  # noqa: E402
from app.notifier import TelegramNotifier, notifier, build_break_timeout  # noqa: E402

_sent: list[str] = []


def _fake_send(self, text: str, parse_mode=None, fallback_text=None) -> None:  # noqa: ANN001
    _sent.append(text)


TelegramNotifier._send = _fake_send
urllib.request.urlopen = lambda *a, **kw: (_ for _ in ()).throw(  # type: ignore[assignment]
    RuntimeError("smoke 严禁真发 Telegram")
)

# ---- in-memory DB ----
from sqlalchemy import create_engine, select  # noqa: E402
from sqlalchemy.orm import sessionmaker  # noqa: E402

from app.database import Base  # noqa: E402
import app.models as models  # noqa: E402

test_engine = create_engine("sqlite:///:memory:", future=True)
Base.metadata.create_all(test_engine)
TestSession = sessionmaker(bind=test_engine, expire_on_commit=False, future=True)

# settings_service / sweeper 用全局 SessionLocal，统一替换为 TestSession
import app.database as db_mod  # noqa: E402
import app.services.settings_service as ss_mod  # noqa: E402
db_mod.SessionLocal = TestSession  # type: ignore[assignment]
ss_mod.SessionLocal = TestSession  # type: ignore[assignment]
import app.notifier as _nm  # noqa: E402
_nm.SessionLocal = TestSession  # type: ignore[assignment]  # 6.5B mention 解析走 in-memory

from app.constants import WorkState  # noqa: E402
from app.services import work_state, sweeper  # noqa: E402

# auto_return 开关用可控替身（不依赖 DB 写入）
_auto_return = {"v": True}
_orig_get = ss_mod.settings_service.get


def _fake_get(key):
    if key == "auto_return_after_break_timeout":
        return _auto_return["v"]
    return _orig_get(key)


ss_mod.settings_service.get = _fake_get

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


def _wipe(db) -> None:
    """清空考勤相关表，保证各 section 之间互不干扰（in-memory DB 跨 session 共享）。"""
    for model in (models.BreakSession, models.ShiftSession,
                  models.EmployeeStatusRow, models.Employee):
        for row in db.scalars(select(model)).all():
            db.delete(row)
    db.commit()


def _mk_overdue_break(db, name: str, break_type: str = "meal") -> tuple[int, int]:
    """建员工 + clock_in + start_break，然后把 break 回退成已超时。返回 (emp_id, break_id)。"""
    emp = models.Employee(
        machine_id=f"m_{name}", name=name, hostname=f"h_{name}",
        status="online", idle_seconds=0, is_active=True,
    )
    db.add(emp)
    db.commit()
    db.refresh(emp)
    work_state.clock_in(db, emp.id)
    work_state.start_break(db, emp.id, break_type)
    brk = db.scalars(
        select(models.BreakSession)
        .where(models.BreakSession.employee_id == emp.id)
        .where(models.BreakSession.ended_at.is_(None))
    ).first()
    # 回退 started_at 到很久以前 + planned_max 小，使其超时
    brk.started_at = models.utcnow() - timedelta(seconds=9999)
    brk.planned_max_seconds = 60
    db.commit()
    return emp.id, brk.id


print("[1] 模板 auto_returned=True")
txt = build_break_timeout("Sam", "toilet", 900, auto_returned=True)
ok("含 系统已自动返回工作岗位", "系统已自动返回工作岗位" in txt, txt)
ok("不含 请员工尽快返回", "请员工尽快返回工作岗位" not in txt)

print("[2] 模板 auto_returned=False")
txt = build_break_timeout("Sam", "toilet", 900, auto_returned=False)
ok("含 休息时间已超过设定限制", "休息时间已超过设定限制" in txt, txt)
ok("含 请员工尽快返回工作岗位", "请员工尽快返回工作岗位" in txt, txt)
ok("不含 系统已自动返回", "系统已自动返回工作岗位" not in txt)

print("[3] find_overdue_breaks 只读（不关闭 / 不改状态）")
with TestSession() as db:
    _wipe(db)
    emp_id, break_id = _mk_overdue_break(db, "E_readonly", "meal")
    overdue = work_state.find_overdue_breaks(db)
    ids = [o.break_id for o in overdue]
    ok("超时 break 被识别", break_id in ids, str(ids))
    # 确认未关闭、状态未变
    brk = db.get(models.BreakSession, break_id)
    row = db.get(models.EmployeeStatusRow, emp_id)
    ok("ended_at 仍为 None（未关闭）", brk.ended_at is None)
    ok("work_state 仍是 break_meal（未回座）", row.work_state == WorkState.break_meal.value, row.work_state)
    o = next(o for o in overdue if o.break_id == break_id)
    ok("OverdueBreak 字段完整", o.employee_name == "E_readonly" and o.break_type == "meal" and o.duration_seconds > 60)

print("[4] sweeper ON 路径：自动回座 + 通知 auto_returned=True")
_auto_return["v"] = True
notifier.reset_cooldowns()
sweeper._off_mode_notified.clear()
_sent.clear()
with TestSession() as db:
    _wipe(db)
    emp_id, break_id = _mk_overdue_break(db, "E_on", "smoke")
    sweeper._sweep_once(db)
    brk = db.get(models.BreakSession, break_id)
    row = db.get(models.EmployeeStatusRow, emp_id)
    ok("ON: break 已关闭 (ended_at 非空)", brk.ended_at is not None)
    ok("ON: work_state 恢复 working", row.work_state == WorkState.working.value, row.work_state)
ok("ON: 发了 1 条通知", len(_sent) == 1, str(len(_sent)))
ok("ON: 通知是 auto_returned=True 文案", _sent and "系统已自动返回工作岗位" in _sent[0])

print("[5] sweeper OFF 路径：保持 break + 通知 auto_returned=False + 仅一次")
_auto_return["v"] = False
notifier.reset_cooldowns()
sweeper._off_mode_notified.clear()
_sent.clear()
with TestSession() as db:
    _wipe(db)
    emp_id, break_id = _mk_overdue_break(db, "E_off", "meal")
    # 第 1 次 tick
    sweeper._sweep_once(db)
    brk = db.get(models.BreakSession, break_id)
    row = db.get(models.EmployeeStatusRow, emp_id)
    ok("OFF: break 仍未关闭", brk.ended_at is None)
    ok("OFF: work_state 仍是 break_meal", row.work_state == WorkState.break_meal.value, row.work_state)
    ok("OFF: 第1次发了 1 条通知", len(_sent) == 1, str(len(_sent)))
    ok("OFF: 通知是 auto_returned=False 文案", _sent and "请员工尽快返回工作岗位" in _sent[0])
    ok("OFF: break_id 进入去重 set", break_id in sweeper._off_mode_notified)
    # 第 2 次 tick（同一 break 仍超时）→ 不应再发
    sweeper._sweep_once(db)
    ok("OFF: 第2次 tick 不重复发（仍 1 条）", len(_sent) == 1, str(len(_sent)))

print("[6] OFF 去重：员工手动回座后 break_id 从 set 剔除")
with TestSession() as db:
    # 手动回座关闭 break
    work_state.return_to_work(db, emp_id)
    # 再 tick：overdue 列表为空 → intersection_update 剔除旧 id
    sweeper._sweep_once(db)
    ok("回座后 break_id 已从去重 set 移除", break_id not in sweeper._off_mode_notified)
    row = db.get(models.EmployeeStatusRow, emp_id)
    ok("回座后 work_state=working", row.work_state == WorkState.working.value, row.work_state)

print()
print("=" * 60)
print(f"PASS={PASS}  FAIL={len(FAIL)}")
if FAIL:
    for label in FAIL:
        print(f"  - {label}")
    print(f"\n_send 调用 {len(_sent)} 次（均被拦截，0 真发）")
    sys.exit(1)
print(f"\nGS-13 全部通过；_send 共拦截 {len(_sent)} 次（0 真发）")

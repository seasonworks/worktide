"""Phase 6.5B · UX-004 冒烟（双击吞重复 noop，**dry-run / 0 真发**）。

    server/.venv/Scripts/python.exe server/scripts/smoke_ux_004.py

覆盖：
    1  punch_debounce 模块边界：mark / is_recent_duplicate / 8s 窗口 / reset / (emp,action) 隔离
    2  Reply 路径：按 Lunch 成功 → 立即再按 Lunch(noop) → 第 2 条被吞（只剩成功卡片）
    3  Reply 路径：成功后超窗(9s)再按 Lunch(noop) → noop 正常发出
    4  Reply 路径：无前置成功的 noop（直接已在用餐）→ noop 正常发出（基线，不误吞）
    5  Reply 路径：不同动作不串扰（mark Lunch 不会吞 Back-To-Work 的 noop）
    6  Inline 路径：noop 仍回 AnswerCallback toast（ephemeral，不受 UX-004 影响）
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
from app.notifier import TelegramNotifier  # noqa: E402

TelegramNotifier._send = lambda self, text, parse_mode=None, fallback_text=None: None  # type: ignore[assignment]
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
_nm.SessionLocal = TestSession  # type: ignore[assignment]

from app.constants import WorkState  # noqa: E402
from app.services import punch_debounce, manual_punch, work_state  # noqa: E402
from app.services import telegram_handlers as th  # noqa: E402

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


def _mk_emp(name: str, work_state_value: str = WorkState.working.value) -> int:
    with TestSession() as db:
        emp = models.Employee(
            machine_id=f"m_{name}", name=name, hostname=f"h_{name}",
            status="online", idle_seconds=0, is_active=True,
        )
        db.add(emp)
        db.commit()
        db.refresh(emp)
        db.add(models.EmployeeStatusRow(
            employee_id=emp.id, work_state=work_state_value, active_break_id=None))
        db.commit()
        return emp.id


def _execute(employee_id: int, name: str, callback_data: str, cq_id: str | None):
    """跑一次 _execute_punch_action，返回 actions 列表。"""
    with TestSession() as db:
        return th._execute_punch_action(
            db, employee_id, name,
            callback_data=callback_data, chat_id=-100123, cq_id=cq_id,
        )


def _send_texts(actions) -> list[str]:
    return [a.text for a in actions if isinstance(a, th.SendMessage)]


def _toast_texts(actions) -> list[str]:
    return [a.text for a in actions if isinstance(a, th.AnswerCallback)]


def _is_success_card(text: str) -> bool:
    return "Current Status" in text and "当前状态" in text


def _is_noop(text: str) -> bool:
    return text.startswith("ℹ️")


print("[1] punch_debounce 模块边界（窗口 8s）")
punch_debounce.reset()
now = datetime.now(timezone.utc)
ok("未 mark → False", punch_debounce.is_recent_duplicate(1, "ws:break:meal", now) is False)
punch_debounce.mark(1, "ws:break:meal", when=now)
ok("刚 mark → True", punch_debounce.is_recent_duplicate(1, "ws:break:meal", now) is True)
ok("+8s → True（边界含）", punch_debounce.is_recent_duplicate(1, "ws:break:meal", now + timedelta(seconds=8)) is True)
ok("+9s → False（超窗）", punch_debounce.is_recent_duplicate(1, "ws:break:meal", now + timedelta(seconds=9)) is False)
ok("不同 action 不命中", punch_debounce.is_recent_duplicate(1, "ws:break:smoke", now) is False)
ok("不同员工不命中", punch_debounce.is_recent_duplicate(2, "ws:break:meal", now) is False)
ok("WINDOW_SECONDS == 8", punch_debounce.WINDOW_SECONDS == 8)
punch_debounce.reset()
ok("reset 后清空", punch_debounce.is_recent_duplicate(1, "ws:break:meal", now) is False)

print("[2] Reply 路径：Lunch 成功 → 立即再按 Lunch → 第 2 条被吞")
punch_debounce.reset(); manual_punch.reset()
eid = _mk_emp("E_dbl", WorkState.working.value)
a1 = _execute(eid, "E_dbl", "ws:break:meal", cq_id=None)
t1 = _send_texts(a1)
ok("第1次：发 1 条且是成功卡片", len(t1) == 1 and _is_success_card(t1[0]), str(t1))
a2 = _execute(eid, "E_dbl", "ws:break:meal", cq_id=None)
t2 = _send_texts(a2)
ok("第2次（双击 noop）：被吞，0 条 SendMessage", len(t2) == 0, str(t2))
# 状态仍是用餐中（无脏数据）
with TestSession() as db:
    row = db.get(models.EmployeeStatusRow, eid)
    ok("work_state 仍是 break_meal（数据正确）", row.work_state == WorkState.break_meal.value, row.work_state)

print("[3] Reply 路径：成功后超窗(9s)再按 → noop 正常发")
punch_debounce.reset(); manual_punch.reset()
eid = _mk_emp("E_late", WorkState.working.value)
_execute(eid, "E_late", "ws:break:meal", cq_id=None)  # 成功 + mark(now)
# 把 mark 回拨到 9s 前，模拟超窗后再点
punch_debounce.mark(eid, "ws:break:meal", when=datetime.now(timezone.utc) - timedelta(seconds=9))
a3 = _execute(eid, "E_late", "ws:break:meal", cq_id=None)
t3 = _send_texts(a3)
ok("超窗 noop 正常发 1 条 ℹ️ 提示", len(t3) == 1 and _is_noop(t3[0]), str(t3))

print("[4] Reply 路径：无前置成功的 noop（已在用餐）→ 正常发（基线）")
punch_debounce.reset(); manual_punch.reset()
eid = _mk_emp("E_base", WorkState.working.value)
_execute(eid, "E_base", "ws:break:meal", cq_id=None)  # 建真实 break + mark
punch_debounce.reset()  # 模拟无最近成功标记（重启后 / 远超窗口）
a4 = _execute(eid, "E_base", "ws:break:meal", cq_id=None)  # 真 noop：break_already_active
t4 = _send_texts(a4)
ok("无 mark 的 noop 不误吞（1 条 ℹ️）", len(t4) == 1 and _is_noop(t4[0]), str(t4))

print("[5] Reply 路径：不同动作不串扰（mark Lunch 不吞 Back 的 noop）")
punch_debounce.reset(); manual_punch.reset()
eid = _mk_emp("E_diff", WorkState.working.value)
_execute(eid, "E_diff", "ws:break:meal", cq_id=None)  # Lunch 成功 → mark(meal)
# 现在在用餐；按「回座」会成功(changed=True) — 为测“不同动作 noop 不串扰”，
# 改造场景：先回到 working，再按 Back（已在工作 → noop），验证未被 meal 的 mark 误吞
_execute(eid, "E_diff", "ws:back", cq_id=None)  # 回座成功
a5 = _execute(eid, "E_diff", "ws:back", cq_id=None)  # 已在工作 → already_returned noop
t5 = _send_texts(a5)
# 注意：上一行 Back 成功也会 mark(back)，所以这条 Back-noop 其实会被 back 自身的 mark 吞；
# 真正要验证的是 meal 的 mark 不影响 back —— 用一个全新员工更干净：
punch_debounce.reset(); manual_punch.reset()
eid = _mk_emp("E_diff2", WorkState.working.value)
punch_debounce.mark(eid, "ws:break:meal")  # 只对 Lunch 打 mark，不对 Back
a5b = _execute(eid, "E_diff2", "ws:back", cq_id=None)  # working→Back = already noop，无 back mark
t5b = _send_texts(a5b)
ok("仅 mark Lunch 时 Back 的 noop 不被吞（1 条 ℹ️）", len(t5b) == 1 and _is_noop(t5b[0]), str(t5b))

print("[6] Inline 路径：noop 仍回 AnswerCallback toast（不受 UX-004 影响）")
punch_debounce.reset(); manual_punch.reset()
eid = _mk_emp("E_inline", WorkState.working.value)
_execute(eid, "E_inline", "ws:break:meal", cq_id="cq1")  # 成功 + mark
a6 = _execute(eid, "E_inline", "ws:break:meal", cq_id="cq2")  # 立即重复（窗口内）
toasts = _toast_texts(a6)
sends = _send_texts(a6)
ok("inline 重复仍回 1 条 toast（不转圈）", len(toasts) == 1, str(toasts))
ok("inline noop 不产生持久 SendMessage", len(sends) == 0, str(sends))

print()
print("=" * 60)
print(f"PASS={PASS}  FAIL={len(FAIL)}")
if FAIL:
    for label in FAIL:
        print(f"  - {label}")
    sys.exit(1)
print("UX-004 全部通过（双击吞重复 noop；0 真发）")

"""Phase 6.5A · GS-12 Action Feedback Improvement 冒烟（**dry-run / 0 真发**）。

    server/.venv/Scripts/python.exe server/scripts/smoke_gs12.py

覆盖 _execute_punch_action 的 changed=False 友好提示分流：
    1-6  用户规格的 6 个场景（Inline toast + Reply SendMessage 两路一致）
    7    not_on_shift（off_shift 点 break）→ 友好提示而非 Operation Failed
    8    未识别 callback_data（result=None）→ 仍 _TOAST_FAIL
    9    成功（changed=True）→ 正常 toast/状态消息（回归）
    10   兜底 code（未列入映射的 noop）→ ℹ️ + 状态机 message
"""
from __future__ import annotations

import os
import sys
from pathlib import Path

if hasattr(sys.stdout, "reconfigure"):
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")

os.environ.setdefault("DATABASE_URL", "sqlite:///:memory:")
os.environ.setdefault("CORS_ORIGINS", "[]")
os.environ.setdefault("TELEGRAM_ENABLED", "false")

_HERE = Path(__file__).resolve()
_SERVER = _HERE.parents[1]
if str(_SERVER) not in sys.path:
    sys.path.insert(0, str(_SERVER))

from app.services import telegram_handlers as th  # noqa: E402
from app.services.work_state import ActionResult  # noqa: E402

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


# 用一个可控的 _dispatch_action 替身：根据传入的 (code, work_state, changed) 返回 ActionResult
_next_result: dict = {"value": None}


def _fake_dispatch(_db, _emp, _data):
    return _next_result["value"]


_orig_dispatch = th._dispatch_action
th._dispatch_action = _fake_dispatch


def run_inline(callback_data: str, result) -> str:
    """模拟 Inline Keyboard 路径，返回 AnswerCallback.text。"""
    _next_result["value"] = result
    actions = th._execute_punch_action(
        db=None, employee_id=1, employee_name="测试",
        callback_data=callback_data, chat_id=None, cq_id="cq1",
    )
    # 只有一个 AnswerCallback
    assert len(actions) == 1, actions
    return actions[0].text


def run_reply(callback_data: str, result) -> list:
    """模拟 Reply Keyboard 路径，返回 actions（SendMessage 列表）。"""
    _next_result["value"] = result
    return th._execute_punch_action(
        db=None, employee_id=1, employee_name="测试",
        callback_data=callback_data, chat_id=999, cq_id=None,
    )


print("[1] Working + Start Work → Already Working")
t = run_inline("ws:clock_in", ActionResult(False, "working", "already_on_shift", "x"))
ok("inline toast = Already Working", "Already Working" in t and "上班状态" in t, t)

print("[2] Off Duty + End Work → Already Off Duty")
t = run_inline("ws:clock_out", ActionResult(False, "off_shift", "already_off_shift", "x"))
ok("inline toast = Already Off Duty", "Already Off Duty" in t and "下班状态" in t, t)

print("[3] Lunch + Lunch → Already On Lunch Break")
t = run_inline("ws:break:meal", ActionResult(False, "break_meal", "break_already_active", "x"))
ok("inline toast = Already On Lunch Break", "Already On Lunch Break" in t and "用餐状态" in t, t)

print("[4] Smoke + Smoke → Already On Smoke Break")
t = run_inline("ws:break:smoke", ActionResult(False, "break_smoke", "break_already_active", "x"))
ok("inline toast = Already On Smoke Break", "Already On Smoke Break" in t and "抽烟状态" in t, t)

print("[5] Restroom + Restroom → Already On Restroom Break")
t = run_inline("ws:break:toilet", ActionResult(False, "break_toilet", "break_already_active", "x"))
ok("inline toast = Already On Restroom Break", "Already On Restroom Break" in t and "如厕状态" in t, t)

print("[6] Working + Back To Work → Already Working / 无需回座")
t = run_inline("ws:back", ActionResult(False, "working", "not_on_break", "x"))
ok("inline toast = Already Working 无需回座", "Already Working" in t and "无需回座" in t, t)

print("[7] off_shift + break → 请先上班（友好，不是 Operation Failed）")
t = run_inline("ws:break:meal", ActionResult(False, "off_shift", "not_on_shift", "x"))
ok("inline toast 含 请先上班", "请先上班" in t and "Operation Failed" not in t, t)

print("[8] 未识别 callback_data (result=None) → 仍 Operation Failed")
t = run_inline("ws:unknown", None)
ok("inline toast = Operation Failed", "Operation Failed" in t, t)

print("[9] 成功 changed=True → 正常 toast（非失败/非 noop）")
t = run_inline("ws:clock_in", ActionResult(True, "working", "clock_in", "ok"))
ok("成功 toast 含 Clock In", "Clock In" in t and "Operation Failed" not in t and "ℹ️" not in t, t)

print("[10] 兜底 noop code → ℹ️ + 状态机 message")
t = run_inline("ws:clock_in", ActionResult(False, "working", "some_future_code", "未来文案"))
ok("兜底 toast = ℹ️ 未来文案", t.startswith("ℹ️") and "未来文案" in t, t)

print("[11] Reply Keyboard 路径：noop → SendMessage 友好提示（非 Operation Failed）")
acts = run_reply("ws:break:meal", ActionResult(False, "break_meal", "break_already_active", "x"))
sm = [a for a in acts if isinstance(a, th.SendMessage)]
ok("reply SendMessage = Already On Lunch Break",
   len(sm) == 1 and "Already On Lunch Break" in sm[0].text, str(acts))

print("[12] Reply Keyboard 路径：成功 → 完整状态消息（回归）")
acts = run_reply("ws:clock_in", ActionResult(True, "working", "clock_in", "ok"))
sm = [a for a in acts if isinstance(a, th.SendMessage)]
ok("reply 成功发状态消息含 员工：测试",
   len(sm) == 1 and "员工：测试" in sm[0].text, str(acts))

th._dispatch_action = _orig_dispatch  # 还原

print()
print("=" * 60)
print(f"PASS={PASS}  FAIL={len(FAIL)}")
if FAIL:
    for label in FAIL:
        print(f"  - {label}")
    sys.exit(1)
print("GS-12 全部通过（无网络调用 / 0 真发）")

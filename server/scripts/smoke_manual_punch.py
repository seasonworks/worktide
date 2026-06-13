"""Phase 6.5A · UX-003 修复冒烟（manual_punch + activity 抑制）。

    .venv\\Scripts\\python.exe server\\scripts\\smoke_manual_punch.py

覆盖：
    1. manual_punch.mark / is_suppressed / reset 基础语义
    2. SUPPRESS_SECONDS 边界行为
    3. telegram_handlers._execute_punch_action(changed=True) → manual_punch.mark
    4. telegram_handlers._execute_punch_action(changed=False) → 不 mark
    5. activity.report idle→active + 60s 内 manual_punch → idle_exit 抑制
    6. activity.report idle→active + 无 manual_punch → idle_exit 触发
"""
from __future__ import annotations

import sys
import urllib.request
from datetime import datetime, timedelta, timezone
from pathlib import Path

if hasattr(sys.stdout, "reconfigure"):
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")

import os
os.environ.setdefault("DATABASE_URL", "sqlite:///:memory:")
os.environ.setdefault("CORS_ORIGINS", "[]")
os.environ.setdefault("TELEGRAM_ENABLED", "false")

_HERE = Path(__file__).resolve()
_SERVER = _HERE.parents[1]
if str(_SERVER) not in sys.path:
    sys.path.insert(0, str(_SERVER))

# 物理切断 Telegram 真发
from app.notifier import TelegramNotifier, notifier  # noqa: E402

_send_calls: list[str] = []


def _fake_send(self, text: str) -> None:  # noqa: ANN001
    _send_calls.append(text.split("\n", 1)[0])


TelegramNotifier._send = _fake_send

_orig_urlopen = urllib.request.urlopen


def _block_urlopen(*a, **kw):
    raise RuntimeError("smoke 严禁真发 Telegram")


urllib.request.urlopen = _block_urlopen

# ------------------------------------------------------------------------------

from app.services import manual_punch  # noqa: E402

PASS, FAIL = 0, []


def ok(label: str, cond: bool, detail: str = "") -> None:
    global PASS
    if cond:
        PASS += 1
        print(f"  ✅ {label}")
    else:
        FAIL.append(label)
        print(f"  ❌ {label}   {detail}")


print("[1] manual_punch 基础语义")
manual_punch.reset()
now = datetime.now(timezone.utc)
ok("初始未 mark → is_suppressed=False", manual_punch.is_suppressed(1, now) is False)
manual_punch.mark(1, when=now)
ok("刚 mark → 当下 is_suppressed=True", manual_punch.is_suppressed(1, now) is True)
ok("刚 mark → +30s is_suppressed=True", manual_punch.is_suppressed(1, now + timedelta(seconds=30)) is True)
ok("刚 mark → +60s is_suppressed=True（边界含）",
   manual_punch.is_suppressed(1, now + timedelta(seconds=60)) is True)
ok("刚 mark → +61s is_suppressed=False",
   manual_punch.is_suppressed(1, now + timedelta(seconds=61)) is False)
ok("其它员工不串扰", manual_punch.is_suppressed(2, now) is False)

print()
print("[2] mark 覆盖既有时间戳")
manual_punch.reset()
manual_punch.mark(1, when=now - timedelta(seconds=120))
ok("旧 mark 已超窗 → is_suppressed=False", manual_punch.is_suppressed(1, now) is False)
manual_punch.mark(1, when=now)
ok("新 mark 覆盖 → is_suppressed=True", manual_punch.is_suppressed(1, now) is True)

print()
print("[3] telegram_handlers._execute_punch_action(changed=True) → mark")
manual_punch.reset()

from app.services import telegram_handlers  # noqa: E402
from app.services.work_state import ActionResult  # noqa: E402

# monkey-patch _dispatch_action 返回 changed=True
_orig_dispatch = telegram_handlers._dispatch_action


def _fake_dispatch_changed(_db, _emp, _data):
    return ActionResult(changed=True, work_state="working", code="ok", message="")


telegram_handlers._dispatch_action = _fake_dispatch_changed
actions = telegram_handlers._execute_punch_action(
    db=None, employee_id=42, employee_name="test",
    callback_data="ws:back", chat_id=999, cq_id=None,
)
ok("changed=True → manual_punch.mark(42) 已登记",
   manual_punch.is_suppressed(42, datetime.now(timezone.utc)) is True)
ok("changed=True → 仍返回正常 chat msg 而非 _TOAST_FAIL",
   len(actions) == 1 and "员工：test" in actions[0].text)

print()
print("[4] telegram_handlers._execute_punch_action(changed=False) → 不 mark")
manual_punch.reset()


def _fake_dispatch_noop(_db, _emp, _data):
    # GS-12 后：not_on_break 这类 noop 走友好提示而非 Operation Failed
    return ActionResult(changed=False, work_state="working", code="not_on_break", message="x")


telegram_handlers._dispatch_action = _fake_dispatch_noop
actions = telegram_handlers._execute_punch_action(
    db=None, employee_id=43, employee_name="test",
    callback_data="ws:back", chat_id=999, cq_id=None,
)
ok("changed=False → manual_punch 未登记",
   manual_punch.is_suppressed(43, datetime.now(timezone.utc)) is False)
# GS-12：noop 不再是 Operation Failed，而是友好 ℹ️ 提示（此处 not_on_break → Already Working）
ok("changed=False → 返回友好提示而非 Operation Failed",
   len(actions) == 1
   and "Operation Failed" not in actions[0].text
   and "Already Working" in actions[0].text)

telegram_handlers._dispatch_action = _orig_dispatch  # 还原

print()
print("[5] activity 抑制：60s 内有 mark → idle_exit 跳过")
manual_punch.reset()
from app.routers import activity  # noqa: E402

activity._reset_transition_state()
_send_calls.clear()
notifier.reset_cooldowns() if hasattr(notifier, "reset_cooldowns") else None

# 模拟 idle → active 转换
ts0 = datetime.now(timezone.utc)
activity._classify_transition(99, "idle", ts0)
# 此刻员工"按下 BTW"
manual_punch.mark(99, when=ts0 + timedelta(seconds=5))
# 客户端下次上报回到 active（10s 后）
ts1 = ts0 + timedelta(seconds=10)
event, started = activity._classify_transition(99, "online", ts1)
ok("event 仍是 exit", event == "exit")
ok("idle_started_at 仍是 ts0", started == ts0)
# 直接调 manual_punch.is_suppressed 模拟 activity 的分支
ok("ts1 在 mark 后 5s 内 → is_suppressed=True",
   manual_punch.is_suppressed(99, ts1) is True)

print()
print("[6] activity 不抑制：无 mark → idle_exit 触发")
manual_punch.reset()
activity._reset_transition_state()
ts0 = datetime.now(timezone.utc)
activity._classify_transition(100, "idle", ts0)
ts1 = ts0 + timedelta(seconds=120)
event, started = activity._classify_transition(100, "online", ts1)
ok("event=exit", event == "exit")
ok("无 manual_punch → is_suppressed=False",
   manual_punch.is_suppressed(100, ts1) is False)

print()
print("[7] activity 窗口外不抑制：mark 超过 60s 不再压制")
manual_punch.reset()
ts0 = datetime.now(timezone.utc)
manual_punch.mark(101, when=ts0)
ts1 = ts0 + timedelta(seconds=75)
ok("75s 后 is_suppressed=False", manual_punch.is_suppressed(101, ts1) is False)

print()
print("=" * 60)
print(f"PASS={PASS}  FAIL={len(FAIL)}")
if FAIL:
    for label in FAIL:
        print(f"  - {label}")
    print(f"\n严禁真发计数：urlopen 拦截 = OK，_send 收集 {len(_send_calls)} 条（应为 0）")
    sys.exit(1)
print(f"\n严禁真发计数：urlopen 拦截 = OK，_send 收集 {len(_send_calls)} 条（应为 0）")
assert len(_send_calls) == 0, "smoke 期间不该出现任何 _send 调用"

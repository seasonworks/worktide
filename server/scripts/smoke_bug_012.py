"""Bug-012 修复冒烟（idle_exit 挂机时长从真实挂机起点算起，含阈值段）。

    server/.venv/Scripts/python.exe server/scripts/smoke_bug_012.py

背景：挂机检测要等空闲累计到阈值才触发 enter，旧代码把挂机起点记成"检测到的时刻"，
导致 idle_exit 的挂机时长少算一整个阈值（如阈值 15min、实际 17min 却报 2min）。
修复：enter 时把起点记成 now - idle_seconds（真实起点）。

覆盖：
    1  enter 把 idle_started 记为 now - idle_seconds（真实起点）
    2  复现用户场景：00:49 enter(已空闲15min) → 00:51 exit → 时长 ≈ 17min（非 2min）
    3  exit 返回的 idle_started == enter 时记下的真实起点
    4  默认 idle_seconds=0（旧式 3 参调用）→ 起点 == now（向后兼容）
    5  状态机分类未变：online→idle=enter / idle→online=exit / idle→offline=None / idle→idle=None
    6  不同员工互不串扰
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

from app.routers import activity as A  # noqa: E402

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


UTC = timezone.utc


def _duration(started: datetime, exit_at: datetime) -> int:
    """复刻 activity.report 里 idle_exit 的算法。"""
    return max(0, int((exit_at - started).total_seconds()))


print("[1] enter 把 idle_started 记为 now - idle_seconds")
A._reset_transition_state()
t_enter = datetime(2026, 6, 11, 0, 49, tzinfo=UTC)
ev, started = A._classify_transition(1, "idle", t_enter, idle_seconds=900)
ok("enter 事件", ev == "enter" and started is None, str((ev, started)))
ok("idle_started == now - 900s（真实起点 00:34）",
   A._idle_started_at[1] == t_enter - timedelta(seconds=900), str(A._idle_started_at[1]))

print("[2] 复现用户场景：00:49 enter(15min) → 00:51 exit → ≈17min")
t_exit = datetime(2026, 6, 11, 0, 51, tzinfo=UTC)
ev2, started2 = A._classify_transition(1, "online", t_exit, idle_seconds=0)
ok("exit 事件", ev2 == "exit", str(ev2))
dur = _duration(started2, t_exit)
ok("挂机时长 == 1020s（17 分钟，含阈值前的 15min）", dur == 1020, f"{dur}s")
ok("不再是旧 bug 的 2 分钟", dur != 120, f"{dur}s")

print("[3] exit 返回的 idle_started == enter 真实起点")
ok("started2 == 00:34", started2 == t_enter - timedelta(seconds=900), str(started2))

print("[4] 默认 idle_seconds=0（旧式 3 参调用，向后兼容）")
A._reset_transition_state()
t0 = datetime(2026, 6, 11, 1, 0, tzinfo=UTC)
A._classify_transition(2, "idle", t0)  # 不传 idle_seconds
ok("起点 == now（默认 0）", A._idle_started_at[2] == t0, str(A._idle_started_at[2]))

print("[5] 状态机分类未变")
A._reset_transition_state()
n = datetime(2026, 6, 11, 2, 0, tzinfo=UTC)
e, _ = A._classify_transition(3, "online", n)
ok("首次 online → 无事件", e is None)
e, _ = A._classify_transition(3, "idle", n + timedelta(seconds=30), idle_seconds=900)
ok("online → idle → enter", e == "enter")
e, _ = A._classify_transition(3, "idle", n + timedelta(seconds=60), idle_seconds=930)
ok("idle → idle → 无事件", e is None)
e, st = A._classify_transition(3, "online", n + timedelta(seconds=90))
ok("idle → online → exit", e == "exit" and st is not None)
A._reset_transition_state()
A._classify_transition(4, "idle", n, idle_seconds=900)
e, _ = A._classify_transition(4, "offline", n + timedelta(seconds=30))
ok("idle → offline 不视为 exit", e is None)

print("[6] 不同员工互不串扰")
A._reset_transition_state()
A._classify_transition(10, "online", n)
A._classify_transition(11, "idle", n, idle_seconds=900)
e, _ = A._classify_transition(10, "idle", n + timedelta(seconds=30), idle_seconds=900)
ok("emp10 enter 不受 emp11 影响", e == "enter")
ok("emp11 起点独立", A._idle_started_at[11] == n - timedelta(seconds=900))

print()
print("=" * 60)
print(f"PASS={PASS}  FAIL={len(FAIL)}")
if FAIL:
    for label in FAIL:
        print(f"  - {label}")
    sys.exit(1)
print("Bug-012 修复全部通过（挂机时长含阈值段，两条消息自洽）")

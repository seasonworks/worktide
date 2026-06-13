"""Phase 6.5A Sprint C · 触发链路冒烟（**严格 dry-run / 0 真发**）。

跑法（项目根目录）：

    .venv\\Scripts\\python.exe server\\scripts\\smoke_sprint_c.py

覆盖 5 个挂载点：
    1. work_state.expire_overdue_breaks(on_timeout=...) — GS-3
    2. work_state.start_break(on_switch=...)           — GS-7B
    3. activity._classify_transition 状态机           — GS-4/GS-5
    4. sweeper._on_break_timeout 真把 notifier 调到   — 接线
    5. telegram_handlers._on_break_switch 真把 notifier 调到 — 接线

设计取舍：
- 业务路径用 SQLite **:memory:** + Base.metadata.create_all 起干净库（不动 dev DB）
- 全局 monkey-patch TelegramNotifier._send + urlopen，物理切断真发
- 测试场景：模拟"Alex事件"（smoke 超时 + 后续上报回到 working）
"""
from __future__ import annotations

import inspect
import sys
import urllib.request
from datetime import datetime, timedelta, timezone
from pathlib import Path

# Windows 控制台 UTF-8（与既有 smoke 同款）
if hasattr(sys.stdout, "reconfigure"):
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")
if hasattr(sys.stderr, "reconfigure"):
    sys.stderr.reconfigure(encoding="utf-8", errors="replace")

_HERE = Path(__file__).resolve()
_SERVER = _HERE.parents[1]
if str(_SERVER) not in sys.path:
    sys.path.insert(0, str(_SERVER))

# ----- 物理切断 Telegram 真发（双保险，与 smoke_notifier.py 一致）-----
from app.notifier import TelegramNotifier, notifier  # noqa: E402

_send_calls: list[tuple[str, str]] = []  # (kind, head)


def _fake_send(self, text: str, parse_mode=None, fallback_text=None) -> None:  # noqa: ANN001
    head = text.split("\n", 1)[0]
    # 用 head 分类（每个模板首行不同）
    if "Break Timeout" in head:
        kind = "break_timeout"
    elif "Break Switched" in head:
        kind = "break_switch"
    elif "🟡" in head:
        kind = "idle_enter"
    elif "🟢" in head:
        kind = "idle_exit"
    elif "Clock-In Reminder" in head:
        kind = "clock_in"
    elif "Clock-Out Reminder" in head:
        kind = "clock_out"
    else:
        kind = "unknown"
    _send_calls.append((kind, head))
    print(f"    [intercepted _send] kind={kind} head={head!r}")


TelegramNotifier._send = _fake_send  # type: ignore[assignment]
urllib.request.urlopen = lambda *a, **kw: (_ for _ in ()).throw(  # type: ignore[assignment]
    RuntimeError("BLOCKED · smoke 禁止 urlopen 出网")
)

# ----- 准备 in-memory DB（不动 dev/生产 DB） -----
from sqlalchemy import create_engine  # noqa: E402
from sqlalchemy.orm import sessionmaker  # noqa: E402

from app.database import Base  # noqa: E402  -- 触发 model 注册
import app.models as models  # noqa: E402,F401

test_engine = create_engine("sqlite:///:memory:", future=True)
Base.metadata.create_all(test_engine)
TestSession = sessionmaker(bind=test_engine, expire_on_commit=False, future=True)

# 6.5B：notifier 解析 mention 时用 SessionLocal 查 telegram_users，patch 到 in-memory
# 避免误连 dev DB（否则可能给消息加 @mention 前缀，破坏首行分类）
import app.notifier as _nm  # noqa: E402
_nm.SessionLocal = TestSession  # type: ignore[assignment]


_pass = 0
_fail = 0
_failures: list[str] = []


def _check(name: str, cond: bool, detail: str = "") -> None:
    global _pass, _fail
    if cond:
        _pass += 1
        print(f"  ✅ {name}")
    else:
        _fail += 1
        msg = f"  ❌ {name}" + (f" -- {detail}" if detail else "")
        print(msg)
        _failures.append(msg)


def _mk_employee(db, name: str = "TestEmp") -> models.Employee:
    """新员工 + machine_id 时间戳保唯一。"""
    import uuid
    emp = models.Employee(
        machine_id=f"SMOKE-{uuid.uuid4().hex[:12]}",
        name=name,
        hostname="SMOKE-HOST",
        status="online",
        idle_seconds=0,
    )
    db.add(emp)
    db.commit()
    db.refresh(emp)
    return emp


# ---------------------------------------------------------------------------
# 1) work_state.expire_overdue_breaks 调 on_timeout
# ---------------------------------------------------------------------------


def section_expire_callback() -> None:
    print("\n" + "=" * 70)
    print("[1] work_state.expire_overdue_breaks 调 on_timeout (GS-3)")
    print("=" * 70)
    from app.services import work_state
    db = TestSession()
    emp = _mk_employee(db, "Alex-Sprint-C")
    now = datetime.now(timezone.utc)
    # 创建 shift + overdue break (planned_max=1s, 已 started 10s 前)
    started = now - timedelta(seconds=10)
    shift = models.ShiftSession(employee_id=emp.id, started_at=started)
    db.add(shift)
    db.flush()
    brk = models.BreakSession(
        employee_id=emp.id, shift_id=shift.id, break_type="smoke",
        started_at=started, planned_max_seconds=1,
        started_from_activity_status="online",
    )
    db.add(brk)
    status = models.EmployeeStatusRow(
        employee_id=emp.id, work_state="break_smoke",
        state_since=started, active_break_id=brk.id,
    )
    db.add(status)
    db.commit()

    calls: list[dict] = []

    def on_timeout(eid: int, name: str, btype: str, dur: int) -> None:
        calls.append({"eid": eid, "name": name, "btype": btype, "dur": dur})

    results = work_state.expire_overdue_breaks(db, on_timeout=on_timeout)
    _check("expire 返回 1 个 ActionResult", len(results) == 1, f"got {len(results)}")
    _check("on_timeout 回调被调用 1 次", len(calls) == 1, f"got {len(calls)}")
    if calls:
        c = calls[0]
        _check("回调 emp_id 正确", c["eid"] == emp.id, str(c))
        _check("回调 name 正确", c["name"] == "Alex-Sprint-C", str(c))
        _check("回调 break_type 正确", c["btype"] == "smoke", str(c))
        _check("回调 duration >= 10s", c["dur"] >= 10, str(c))

    # 二次调用：break 已关闭 → 无超时 → 回调不再触发
    calls.clear()
    results2 = work_state.expire_overdue_breaks(db, on_timeout=on_timeout)
    _check("二次 expire 不再触发回调", len(calls) == 0)
    _check("二次 expire 返回空 list", results2 == [])
    db.close()


# ---------------------------------------------------------------------------
# 2) work_state.start_break 调 on_switch (异型) / 不调 (同型 + 首次)
# ---------------------------------------------------------------------------


def section_switch_callback() -> None:
    print("\n" + "=" * 70)
    print("[2] work_state.start_break 调 on_switch (GS-7B)")
    print("=" * 70)
    from app.constants import BreakType
    from app.services import work_state
    db = TestSession()
    emp = _mk_employee(db, "Jordan-Sprint-C")

    # 必须先 clock_in（off_shift 不能开 break）
    work_state.clock_in(db, emp.id)

    calls: list[tuple] = []

    def on_switch(eid: int, name: str, frm: str, to: str) -> None:
        calls.append((eid, name, frm, to))

    # 首次 meal: 不应触发 on_switch（没有前一个 break）
    r1 = work_state.start_break(db, emp.id, BreakType.meal, on_switch=on_switch)
    _check("首次 meal 成功", r1.changed and r1.code == "break_started")
    _check("首次 break 不触发 on_switch", len(calls) == 0)

    # 切换到 smoke: 应触发 on_switch(emp, name, meal, smoke)
    r2 = work_state.start_break(db, emp.id, BreakType.smoke, on_switch=on_switch)
    _check("切换 → smoke 成功", r2.changed and r2.code == "break_switched")
    _check("on_switch 触发 1 次", len(calls) == 1, str(calls))
    if calls:
        _check("on_switch 参数 (eid, name, meal, smoke)",
               calls[0] == (emp.id, "Jordan-Sprint-C", "meal", "smoke"),
               str(calls[0]))

    # 再切回 smoke（同型）: 应 noop, 不触发 on_switch
    calls.clear()
    r3 = work_state.start_break(db, emp.id, BreakType.smoke, on_switch=on_switch)
    _check("同型 smoke→smoke noop", r3.changed is False and r3.code == "break_already_active")
    _check("noop 不触发 on_switch", len(calls) == 0)
    db.close()


# ---------------------------------------------------------------------------
# 3) activity._classify_transition 状态机矩阵
# ---------------------------------------------------------------------------


def section_classify_matrix() -> None:
    print("\n" + "=" * 70)
    print("[3] activity._classify_transition 状态机（GS-4 / GS-5）")
    print("=" * 70)
    from app.routers.activity import _classify_transition, _reset_transition_state

    _reset_transition_state()
    now = datetime.now(timezone.utc)

    # 首次 online: 无事件
    e, st = _classify_transition(1, "online", now)
    _check("首次 online → 无事件", e is None and st is None)

    # online → idle: enter
    now2 = now + timedelta(seconds=30)
    e, st = _classify_transition(1, "idle", now2)
    _check("online → idle → enter", e == "enter")

    # idle → idle: 无事件
    now3 = now2 + timedelta(seconds=30)
    e, st = _classify_transition(1, "idle", now3)
    _check("idle → idle → 无事件", e is None)

    # idle → online: exit (返回 idle_started)
    now4 = now3 + timedelta(seconds=600)
    e, st = _classify_transition(1, "online", now4)
    _check("idle → online → exit", e == "exit")
    _check("exit 返回 idle_started_at", st == now2, f"got {st}")
    # 时长 ~10 分钟
    duration = int((now4 - st).total_seconds()) if st else 0
    _check("exit 时长 = ~630s（含 enter 后到 exit）", duration == 630, str(duration))

    # idle → offline 不算 exit
    _reset_transition_state()
    _classify_transition(2, "idle", now)
    e, st = _classify_transition(2, "offline", now + timedelta(seconds=10))
    _check("idle → offline 不视为 exit", e is None)

    # offline → idle 仍触发 enter
    _reset_transition_state()
    _classify_transition(3, "offline", now)
    e, st = _classify_transition(3, "idle", now + timedelta(seconds=10))
    _check("offline → idle → enter", e == "enter")

    # 不同 employee 互不串扰
    _reset_transition_state()
    _classify_transition(10, "online", now)
    _classify_transition(11, "idle", now)
    e, _ = _classify_transition(10, "idle", now + timedelta(seconds=30))
    _check("不同 emp 状态隔离", e == "enter")


# ---------------------------------------------------------------------------
# 4) sweeper 接线 — 真实 import + 调用，验证 _send 被打到
# ---------------------------------------------------------------------------


def section_sweeper_wiring() -> None:
    print("\n" + "=" * 70)
    print("[4] sweeper → notifier 接线（_send 被拦截，0 真发）")
    print("=" * 70)
    from app.services import sweeper, work_state

    # 4a) 静态校验：sweeper 清扫路径用了 on_timeout
    #     GS-13 后清扫逻辑从 _run 抽到 _sweep_once（ON 路径调 expire_overdue_breaks(on_timeout=...)）
    src = inspect.getsource(sweeper._sweep_once)
    _check("sweeper._sweep_once 内含 on_timeout=", "on_timeout=" in src)
    _check("sweeper 文件含 _on_break_timeout 函数", hasattr(sweeper, "_on_break_timeout"))

    # 4b) 动态校验：手工触发一次 expire 用 sweeper._on_break_timeout 作回调
    notifier.reset_cooldowns()
    before = len([c for c in _send_calls if c[0] == "break_timeout"])

    db = TestSession()
    emp = _mk_employee(db, "SWEEP-WIRE")
    now = datetime.now(timezone.utc)
    started = now - timedelta(seconds=10)
    shift = models.ShiftSession(employee_id=emp.id, started_at=started)
    db.add(shift)
    db.flush()
    brk = models.BreakSession(
        employee_id=emp.id, shift_id=shift.id, break_type="meal",
        started_at=started, planned_max_seconds=1,
        started_from_activity_status="online",
    )
    db.add(brk)
    db.add(models.EmployeeStatusRow(
        employee_id=emp.id, work_state="break_meal",
        state_since=started, active_break_id=brk.id,
    ))
    db.commit()
    work_state.expire_overdue_breaks(db, on_timeout=sweeper._on_break_timeout)
    after = len([c for c in _send_calls if c[0] == "break_timeout"])
    _check("sweeper 回调真把 notifier.notify_break_timeout 打到 _send",
           after == before + 1, f"before={before} after={after}")
    db.close()


# ---------------------------------------------------------------------------
# 5) telegram_handlers 接线
# ---------------------------------------------------------------------------


def section_handlers_wiring() -> None:
    print("\n" + "=" * 70)
    print("[5] telegram_handlers → notifier 接线（_send 被拦截，0 真发）")
    print("=" * 70)
    from app.constants import BreakType
    from app.services import telegram_handlers, work_state

    # 5a) 静态校验
    src = inspect.getsource(telegram_handlers._dispatch_action)
    _check("_dispatch_action 内含 on_switch=", "on_switch=" in src)
    _check("handlers 文件含 _on_break_switch 函数",
           hasattr(telegram_handlers, "_on_break_switch"))

    # 5b) 动态校验：触发一次 switched 路径
    notifier.reset_cooldowns()
    before = len([c for c in _send_calls if c[0] == "break_switch"])

    db = TestSession()
    emp = _mk_employee(db, "HANDLER-WIRE")
    work_state.clock_in(db, emp.id)
    # 用 _dispatch_action 走 telegram_handlers 真路径
    telegram_handlers._dispatch_action(db, emp.id, "ws:break:meal")        # 首次
    telegram_handlers._dispatch_action(db, emp.id, "ws:break:smoke")       # switched
    after = len([c for c in _send_calls if c[0] == "break_switch"])
    _check("handlers 路径触发 notify_break_switch 1 次",
           after == before + 1, f"before={before} after={after}")
    db.close()


# ---------------------------------------------------------------------------
# 6) Alex事件复盘：smoke 超时 → 自动结束 → emp 上报 online → 应该获得 GS-5 idle_exit?
#    实际场景：smoke 期间 paused=True，idle_enter 不触发；smoke 超时 sweeper 关闭，
#    随后 emp 上报 online。我们这里直接用 _classify_transition 走流程
# ---------------------------------------------------------------------------


def section_idle_replay() -> None:
    print("\n" + "=" * 70)
    print("[6] 'Alex事件' 路径复盘 — GS-3 现在会有通知")
    print("=" * 70)
    from app.routers.activity import _classify_transition, _reset_transition_state
    from app.services import sweeper, work_state

    _reset_transition_state()
    notifier.reset_cooldowns()
    pre_timeout = len([c for c in _send_calls if c[0] == "break_timeout"])

    db = TestSession()
    emp = _mk_employee(db, "Alex-Replay")
    now = datetime.now(timezone.utc)
    started = now - timedelta(seconds=648)  # ≈ Alex的 648s
    shift = models.ShiftSession(employee_id=emp.id, started_at=started)
    db.add(shift)
    db.flush()
    brk = models.BreakSession(
        employee_id=emp.id, shift_id=shift.id, break_type="smoke",
        started_at=started, planned_max_seconds=600,    # smoke 默认 10min
        started_from_activity_status="online",
    )
    db.add(brk)
    db.add(models.EmployeeStatusRow(
        employee_id=emp.id, work_state="break_smoke",
        state_since=started, active_break_id=brk.id,
    ))
    db.commit()
    work_state.expire_overdue_breaks(db, on_timeout=sweeper._on_break_timeout)
    post_timeout = len([c for c in _send_calls if c[0] == "break_timeout"])
    _check("[Alex事件] sweeper 触发 GS-3 通知（之前没有，现在有）",
           post_timeout == pre_timeout + 1)
    db.close()


# ---------------------------------------------------------------------------
# main
# ---------------------------------------------------------------------------


def main() -> int:
    section_expire_callback()
    section_switch_callback()
    section_classify_matrix()
    section_sweeper_wiring()
    section_handlers_wiring()
    section_idle_replay()

    intercepted = len(_send_calls)
    by_kind: dict[str, int] = {}
    for k, _ in _send_calls:
        by_kind[k] = by_kind.get(k, 0) + 1

    print("\n" + "=" * 70)
    print(f"=== 结果：{_pass} pass / {_fail} fail ===")
    print(f"=== _send 被拦截 {intercepted} 次（按类型: {by_kind}）===")
    print("=== 真实 urlopen 已替换为 raise，0 次实际出网 ===")
    if _failures:
        print("\n失败用例：")
        for m in _failures:
            print(m)
    return 0 if _fail == 0 else 1


if __name__ == "__main__":
    sys.exit(main())

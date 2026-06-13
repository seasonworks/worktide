"""Phase 6.5A Sprint D · Clock Reminder Scheduler 冒烟（严格 dry-run）。

跑法（项目根目录）：

    .venv\\Scripts\\python.exe server\\scripts\\smoke_sprint_d.py

覆盖：
- 时刻命中判定：HH:MM == 配置 → fire；不等 → noop
- 当天幂等：同分钟多 tick → 只 fire 一次
- 目标员工筛选：clock_in → off_shift；clock_out → working/break_*
- 子开关 OFF → 跳过
- 时区偏移：reminders_timezone_offset_hours 改 → tick 用新偏移
- notifier 真被打到（_send 拦截器计数）

护栏（同 B/C smoke）：
- TelegramNotifier._send monkey-patch
- urllib.request.urlopen = raise
- 用 :memory: SQLite，不动 dev / 生产 DB
"""
from __future__ import annotations

import sys
import urllib.request
from datetime import datetime, timedelta, timezone
from pathlib import Path

if hasattr(sys.stdout, "reconfigure"):
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")
if hasattr(sys.stderr, "reconfigure"):
    sys.stderr.reconfigure(encoding="utf-8", errors="replace")

_HERE = Path(__file__).resolve()
_SERVER = _HERE.parents[1]
if str(_SERVER) not in sys.path:
    sys.path.insert(0, str(_SERVER))

# ----- 物理切断 Telegram -----
from app.notifier import TelegramNotifier, notifier  # noqa: E402

_send_calls: list[tuple[str, str]] = []


def _fake_send(self, text: str, parse_mode=None, fallback_text=None) -> None:  # noqa: ANN001
    head = text.split("\n", 1)[0]
    kind = "clock_in" if "Clock-In Reminder" in head else (
        "clock_out" if "Clock-Out Reminder" in head else "other"
    )
    _send_calls.append((kind, head))
    print(f"    [intercepted _send] kind={kind} head={head!r}")


TelegramNotifier._send = _fake_send  # type: ignore[assignment]
urllib.request.urlopen = lambda *a, **kw: (_ for _ in ()).throw(  # type: ignore[assignment]
    RuntimeError("BLOCKED · smoke 禁止 urlopen")
)

# ----- In-memory DB（不动 dev / 生产）-----
from sqlalchemy import create_engine  # noqa: E402
from sqlalchemy.orm import sessionmaker  # noqa: E402

from app.database import Base  # noqa: E402  -- 触发 model 注册
import app.models as models  # noqa: E402,F401

test_engine = create_engine("sqlite:///:memory:", future=True)
Base.metadata.create_all(test_engine)
TestSession = sessionmaker(bind=test_engine, expire_on_commit=False, future=True)

# Monkey-patch SessionLocal 让 scheduler & settings_service 都走 in-memory
# （settings_service._ensure_loaded 也用 module-level SessionLocal，不 patch 它
#  smoke 的 settings 覆盖会写到 in-memory 但读会读到 dev DB —— 互不一致）
import app.services.clock_reminder_scheduler as crs_mod  # noqa: E402
import app.services.settings_service as ss_mod  # noqa: E402
crs_mod.SessionLocal = TestSession  # type: ignore[assignment]
ss_mod.SessionLocal = TestSession   # type: ignore[assignment]
import app.notifier as _nm  # noqa: E402
_nm.SessionLocal = TestSession  # type: ignore[assignment]

# GS-11 后默认 clock_reminder_mode=group；本 smoke 校验的是 individual（逐人）路径，
# 强制 individual，使既有断言（per-employee 发送）继续有效。group 路径由 smoke_gs11 覆盖。
_orig_get_d = ss_mod.settings_service.get


def _force_individual_get(key):
    if key == "clock_reminder_mode":
        return "individual"
    return _orig_get_d(key)


ss_mod.settings_service.get = _force_individual_get

from app.services.clock_reminder_scheduler import (  # noqa: E402
    ClockReminderScheduler,
    _select_clock_in_targets,
    _select_clock_out_targets,
)
from app.services.settings_service import settings_service  # noqa: E402

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


def _mk_emp_with_state(db, name: str, work_state: str) -> int:
    """造一个 active 员工 + EmployeeStatusRow。"""
    import uuid
    emp = models.Employee(
        machine_id=f"D-SMOKE-{uuid.uuid4().hex[:10]}",
        name=name, hostname="D-HOST", status="online", idle_seconds=0,
    )
    db.add(emp)
    db.commit()
    db.refresh(emp)
    status = models.EmployeeStatusRow(
        employee_id=emp.id, work_state=work_state,
        state_since=datetime.now(timezone.utc),
    )
    db.add(status)
    db.commit()
    return emp.id


def _override_settings(**kv) -> None:
    """临时用 settings DB 覆盖（每条用例完后 reset_all）。"""
    db = TestSession()
    try:
        for k, v in kv.items():
            settings_service.update(db, k, v)
    finally:
        db.close()


def _reset_all_settings() -> None:
    db = TestSession()
    try:
        settings_service.reset_all(db)
    finally:
        db.close()


# ---------------------------------------------------------------------------
# 1) 目标员工筛选
# ---------------------------------------------------------------------------


def section_target_filter() -> None:
    print("\n" + "=" * 70)
    print("[1] 目标员工筛选（review 锁定 work_state 维度）")
    print("=" * 70)
    db = TestSession()
    e_off = _mk_emp_with_state(db, "OFF-A", "off_shift")
    e_work = _mk_emp_with_state(db, "WORK-B", "working")
    e_smoke = _mk_emp_with_state(db, "SMOKE-C", "break_smoke")
    e_meal = _mk_emp_with_state(db, "MEAL-D", "break_meal")

    ci = _select_clock_in_targets(db)
    co = _select_clock_out_targets(db)
    ci_ids = {x[0] for x in ci}
    co_ids = {x[0] for x in co}

    _check("clock_in 包含 off_shift 员工", e_off in ci_ids)
    _check("clock_in 不包含 working 员工", e_work not in ci_ids)
    _check("clock_in 不包含 break_smoke 员工", e_smoke not in ci_ids)
    _check("clock_out 包含 working 员工", e_work in co_ids)
    _check("clock_out 包含 break_smoke 员工", e_smoke in co_ids)
    _check("clock_out 包含 break_meal 员工", e_meal in co_ids)
    _check("clock_out 不包含 off_shift 员工", e_off not in co_ids)

    # 离职员工不应进任一组
    emp_inactive = models.Employee(
        machine_id="D-SMOKE-INACT", name="INACTIVE", status="online",
        is_active=False, idle_seconds=0,
    )
    db.add(emp_inactive)
    db.commit()
    ci2 = _select_clock_in_targets(db)
    _check("离职员工不在 clock_in 列表",
           emp_inactive.id not in {x[0] for x in ci2})

    # 没有 EmployeeStatusRow 的员工默认 off_shift → 进 clock_in
    emp_no_state = models.Employee(
        machine_id="D-SMOKE-NS", name="NO-STATE", status="online",
        is_active=True, idle_seconds=0,
    )
    db.add(emp_no_state)
    db.commit()
    ci3 = _select_clock_in_targets(db)
    _check("无 EmployeeStatusRow 默认 off_shift → 在 clock_in 列表",
           emp_no_state.id in {x[0] for x in ci3})
    db.close()


# ---------------------------------------------------------------------------
# 2) 时刻命中：HH:MM 等于配置时刻 → fire；否则 noop
# ---------------------------------------------------------------------------


def section_time_match() -> None:
    print("\n" + "=" * 70)
    print("[2] 时刻命中判定")
    print("=" * 70)
    sch = ClockReminderScheduler()
    # 用 reset_all 清干净，确保默认 9:00 / 18:00
    _reset_all_settings()
    # 打开 clock_in 子开关
    _override_settings(notify_clock_in_reminder_enabled=True,
                       notify_clock_out_reminder_enabled=True)
    notifier.reset_cooldowns()
    sch.reset_state()

    base = len(_send_calls)
    # 假设当前是 08:59（不命中）
    sch.tick(datetime(2026, 6, 2, 8, 59))
    _check("08:59 不命中 09:00 → 无 send", len(_send_calls) == base)

    # 09:00 命中
    res = sch.tick(datetime(2026, 6, 2, 9, 0))
    _check("09:00 命中 → 有 send",
           any(c[0] == "clock_in" for c in _send_calls[base:]),
           f"got {_send_calls[base:]}")
    _check("tick 返回 checked_at_local", res.get("checked_at_local"))

    # 同一天 09:00 再次 tick → 幂等不重发
    snapshot = len(_send_calls)
    sch.tick(datetime(2026, 6, 2, 9, 0))
    _check("同日 09:00 重复 tick 幂等不重发",
           len(_send_calls) == snapshot)

    # 跨天再 09:00 → 应再次发
    # 注意：notifier.CLOCK_IN_REMINDER 有 30min cooldown，否则同 emp 第二次 fire 被 notifier 拦
    notifier.reset_cooldowns()
    sch.tick(datetime(2026, 6, 3, 9, 0))
    _check("跨天 09:00 再次 fire",
           len(_send_calls) > snapshot)

    # 18:00 命中 clock_out
    snapshot2 = len(_send_calls)
    sch.tick(datetime(2026, 6, 3, 18, 0))
    _check("18:00 命中 clock_out → 有 send",
           any(c[0] == "clock_out" for c in _send_calls[snapshot2:]))
    _reset_all_settings()


# ---------------------------------------------------------------------------
# 3) 子开关 OFF → 跳过
# ---------------------------------------------------------------------------


def section_disabled_gate() -> None:
    print("\n" + "=" * 70)
    print("[3] 子开关 OFF → 不发")
    print("=" * 70)
    sch = ClockReminderScheduler()
    _reset_all_settings()
    # 默认就是 OFF（review #4）
    notifier.reset_cooldowns()
    base = len(_send_calls)
    sch.tick(datetime(2026, 6, 2, 9, 0))
    _check("默认 OFF · 09:00 也不发", len(_send_calls) == base)
    sch.tick(datetime(2026, 6, 2, 18, 0))
    _check("默认 OFF · 18:00 也不发", len(_send_calls) == base)


# ---------------------------------------------------------------------------
# 4) 时区偏移可调
# ---------------------------------------------------------------------------


def section_tz_override() -> None:
    print("\n" + "=" * 70)
    print("[4] 时区偏移可改（reminders_timezone_offset_hours）")
    print("=" * 70)
    sch = ClockReminderScheduler()
    _reset_all_settings()
    _override_settings(notify_clock_in_reminder_enabled=True,
                       reminders_timezone_offset_hours=0)
    notifier.reset_cooldowns()
    base = len(_send_calls)
    # 现在 reminder_time=09:00 (默认), tz_offset=0
    # 模拟 UTC 09:00 == local 09:00（因为 offset=0）→ 应发
    sch.reset_state()
    sch.tick(datetime(2026, 6, 4, 9, 0))   # local time = 9:00 (UTC+0)
    _check("offset=0 · 09:00 命中",
           any(c[0] == "clock_in" for c in _send_calls[base:]),
           f"got {_send_calls[base:]}")
    _reset_all_settings()


# ---------------------------------------------------------------------------
# 5) 端到端 fire 计数对账（清晰汇总）
# ---------------------------------------------------------------------------


def section_e2e_summary() -> None:
    print("\n" + "=" * 70)
    print("[5] 端到端汇总（先清空前面 section 留下的 emp）")
    print("=" * 70)
    sch = ClockReminderScheduler()
    _reset_all_settings()
    notifier.reset_cooldowns()

    # 先把 section 1 留下的员工全删，避免计数干扰
    db = TestSession()
    db.query(models.EmployeeStatusRow).delete()
    db.query(models.Employee).delete()
    db.commit()

    # 造员工：3 个 off_shift + 2 个 working
    for i in range(3):
        _mk_emp_with_state(db, f"OFF-E2E-{i}", "off_shift")
    for i in range(2):
        _mk_emp_with_state(db, f"WORK-E2E-{i}", "working")
    db.close()

    _override_settings(notify_clock_in_reminder_enabled=True,
                       notify_clock_out_reminder_enabled=True)
    sch.reset_state()
    base = len(_send_calls)

    # 09:00 → 应发 3 条 clock_in
    res_in = sch.tick(datetime(2026, 6, 5, 9, 0))
    ci_count = sum(1 for c in _send_calls[base:] if c[0] == "clock_in")
    _check(f"09:00 应发 3 条 clock_in，实发 {ci_count}", ci_count == 3)
    _check("tick 返回 fired_for 列表",
           len(res_in.get("clock_in_fired_for", [])) == 3)

    base2 = len(_send_calls)
    # 18:00 → 应发 2 条 clock_out（含上面 3 + 2 = 5 个员工里只有 working 的 2 个）
    res_out = sch.tick(datetime(2026, 6, 5, 18, 0))
    co_count = sum(1 for c in _send_calls[base2:] if c[0] == "clock_out")
    _check(f"18:00 应发 2 条 clock_out，实发 {co_count}", co_count == 2)
    _check("tick 返回 fired_for 列表",
           len(res_out.get("clock_out_fired_for", [])) == 2)

    _reset_all_settings()


# ---------------------------------------------------------------------------
# main
# ---------------------------------------------------------------------------


def main() -> int:
    section_target_filter()
    section_time_match()
    section_disabled_gate()
    section_tz_override()
    section_e2e_summary()

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

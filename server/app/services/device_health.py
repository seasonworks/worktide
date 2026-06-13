"""Phase 5.3 · Device Health service。

职责：
1. ``upsert_health(db, payload)`` —— 按 ``machine_id`` upsert（D3：latest-only，
   不留 history；trend 需求等 Phase 5.4 通过专门的 history 表满足）
2. ``compute_status(row, employee_last_seen, now)`` —— 5 档状态推断
   （D6：状态规则在 server 单点维护，client 只报原始信号）
3. ``list_devices`` / ``get_device_detail`` —— Admin UI 列表 / 详情聚合

状态规则（与设计稿 §3 一致）：

    offline   : last_seen ≥ 5 min
    stale     : 60 s ≤ last_seen < 5 min
    unstable  : last_seen < 60 s 且 last_exit_reason 以 watchdog_ 开头
                且 last_start_at 在 10 min 之内
    degraded  : last_seen < 60 s 且 (last_exit_reason 以 watchdog_ 开头
                或 watchdog.misses 任一 > 0)
    healthy   : last_seen < 60 s 且 last_exit_reason ∈ {initial, normal}
                且 watchdog.misses 全 0

``last_seen = max(employees.last_seen, device_health.last_report_at)``
"""
from __future__ import annotations

from datetime import datetime, timezone

from sqlalchemy import select
from sqlalchemy.orm import Session

from ..models import DeviceHealth, Employee
from ..schemas import (
    DeviceDetailOut,
    DeviceListItemOut,
    HealthReportIn,
    HealthWatchdogIn,
)

# 状态推断阈值。改这里全局生效（D6）。
STALE_AFTER_SECONDS = 60
OFFLINE_AFTER_SECONDS = 300
UNSTABLE_RECENT_RESTART_SECONDS = 600

#: 视为 "正常路径" 的退出原因。其它非 watchdog_ 前缀的 reason 会兜底标 degraded。
#: ``update_pending`` 是 Phase 5.4 加入：agent 主动 exit 让 updater 接管 —
#: 这是预期路径，不应让 status 跌出 healthy。
_HEALTHY_REASONS = {"initial", "normal", "update_pending"}


def _ensure_utc(dt: datetime | None) -> datetime | None:
    """SQLite 把 ``DateTime(timezone=True)`` 读出后 tzinfo 会丢失，
    与 ``datetime.now(timezone.utc)`` 直接相减会抛
    ``can't subtract offset-naive and offset-aware``。
    本函数把所有从 DB 读出的 datetime 统一标记为 UTC（DB 里存的语义就是 UTC，
    只是 SQLite 不持久化 tzinfo）。
    """
    if dt is None:
        return None
    if dt.tzinfo is None:
        return dt.replace(tzinfo=timezone.utc)
    return dt


# ---------------------------------------------------------------------------
# upsert
# ---------------------------------------------------------------------------


def upsert_health(db: Session, payload: HealthReportIn) -> DeviceHealth:
    """按 ``machine_id`` 写入或更新设备健康快照。

    通过 ``machine_id`` 反查 ``Employee.id``：
    - 找到 → ``employee_id`` 自动填上
    - 找不到（machine_id 尚未由 /activity/report 注册）→ employee_id 保留 NULL，
      health 仍然落库（先把状态记下来，等员工注册后通过 join 自动可见）
    """
    row = db.scalar(
        select(DeviceHealth).where(DeviceHealth.machine_id == payload.machine_id)
    )
    employee = db.scalar(
        select(Employee).where(Employee.machine_id == payload.machine_id)
    )
    employee_id = employee.id if employee else None
    now = datetime.now(timezone.utc)

    # watchdog 子结构原样落库（避免反复解构/重组的字段同步成本）
    wd_dict = payload.watchdog.model_dump() if payload.watchdog else None

    if row is None:
        row = DeviceHealth(
            machine_id=payload.machine_id,
            employee_id=employee_id,
            hostname=payload.hostname,
            agent_version=payload.agent_version,
            process_started_at=payload.process_started_at,
            uptime_seconds=payload.uptime_seconds,
            first_start_at=payload.first_start_at,
            last_start_at=payload.last_start_at,
            last_exit_at=payload.last_exit_at,
            last_exit_reason=payload.last_exit_reason,
            restart_count=payload.restart_count,
            pending_events=payload.pending_events,
            watchdog_json=wd_dict,
            last_report_at=now,
        )
        db.add(row)
    else:
        row.employee_id = employee_id
        row.hostname = payload.hostname
        row.agent_version = payload.agent_version
        row.process_started_at = payload.process_started_at
        row.uptime_seconds = payload.uptime_seconds
        row.first_start_at = payload.first_start_at
        row.last_start_at = payload.last_start_at
        row.last_exit_at = payload.last_exit_at
        row.last_exit_reason = payload.last_exit_reason
        row.restart_count = payload.restart_count
        row.pending_events = payload.pending_events
        row.watchdog_json = wd_dict
        row.last_report_at = now

    # Phase 5.4: update 子结构按需覆写。
    # payload.update is None（老 client 不带）→ 不动现有 update_* 字段；
    # 所有非 None 字段都覆写（包括 None 显式清空 last_error 等）。
    if payload.update is not None:
        row.update_status = payload.update.status or "idle"
        row.update_target_version = payload.update.target_version
        if payload.update.last_check_at is not None:
            row.update_last_check_at = payload.update.last_check_at
        row.update_last_error = payload.update.last_error

    db.commit()
    db.refresh(row)
    return row


def touch_update_last_check(db: Session, machine_id: str) -> None:
    """Phase 5.4 · /agent/updates/check 顺手更新 last_check_at。

    让 UI 能看到 client 在轮询。**不动** update_status / target_version /
    last_error（那些只由 HealthReporter 上报路径写）。
    machine_id 不存在 → no-op（client 可能首次 check，health 还没上报过）。
    """
    row = db.scalar(
        select(DeviceHealth).where(DeviceHealth.machine_id == machine_id)
    )
    if row is None:
        return
    row.update_last_check_at = datetime.now(timezone.utc)
    db.commit()


# ---------------------------------------------------------------------------
# 状态推断（D6：server 单点真理）
# ---------------------------------------------------------------------------


def _watchdog_any_miss(watchdog_json: dict | None) -> bool:
    """watchdog.misses 任一 > 0 → True。容错：字段缺失视为 0。"""
    if not isinstance(watchdog_json, dict):
        return False
    misses = watchdog_json.get("misses")
    if not isinstance(misses, dict):
        return False
    return any(int(v or 0) > 0 for v in misses.values())


def compute_status(
    row: DeviceHealth,
    employee_last_seen: datetime | None,
    now: datetime,
) -> str:
    """5 档状态推断。

    :param row: DeviceHealth ORM 行（必须非空，调用方保证）
    :param employee_last_seen: ``employees.last_seen``（来自 /activity 上报）
    :param now: 当前 UTC 时间，注入便于测试
    """
    last_seen = _resolve_last_seen(row, employee_last_seen)
    if last_seen is None:
        # 没有任何 last_seen 信号 —— 把 last_report_at 作为兜底
        last_seen = _ensure_utc(row.last_report_at)
    age_seconds = (now - last_seen).total_seconds() if last_seen else float("inf")

    if age_seconds >= OFFLINE_AFTER_SECONDS:
        return "offline"
    if age_seconds >= STALE_AFTER_SECONDS:
        return "stale"

    # 以下都是 "online"（last_seen < 60s）分支
    reason = row.last_exit_reason or "initial"
    is_watchdog_exit = reason.startswith("watchdog_")

    last_start_at = _ensure_utc(row.last_start_at)
    if is_watchdog_exit and last_start_at is not None:
        since_start = (now - last_start_at).total_seconds()
        if since_start < UNSTABLE_RECENT_RESTART_SECONDS:
            return "unstable"

    if is_watchdog_exit or _watchdog_any_miss(row.watchdog_json):
        return "degraded"

    if reason in _HEALTHY_REASONS:
        return "healthy"

    # 兜底：未知 exit reason，保守标 degraded 让运维注意
    return "degraded"


def _resolve_last_seen(
    row: DeviceHealth, employee_last_seen: datetime | None
) -> datetime | None:
    """``max(employee.last_seen, device_health.last_report_at)``，None 自动排除。

    经过 ``_ensure_utc`` 标记，避免与 ``now`` (UTC aware) 比较时 TypeError。
    """
    candidates = [
        _ensure_utc(t)
        for t in (employee_last_seen, row.last_report_at)
        if t is not None
    ]
    return max(candidates) if candidates else None


# ---------------------------------------------------------------------------
# 查询：列表 / 详情
# ---------------------------------------------------------------------------


def list_devices(
    db: Session, *, include_inactive: bool = False
) -> list[DeviceListItemOut]:
    """全员设备健康列表，按 last_report_at desc 排序。

    与其它列表端点一致：``include_inactive=False`` 时归档员工不显示。
    没有 employee_id（health 上报先于 activity 注册）的行**总是**展示。
    """
    rows = db.scalars(
        select(DeviceHealth).order_by(DeviceHealth.last_report_at.desc())
    ).all()
    now = datetime.now(timezone.utc)
    out: list[DeviceListItemOut] = []
    for row in rows:
        emp = db.get(Employee, row.employee_id) if row.employee_id else None
        if not include_inactive and emp is not None and not emp.is_active:
            continue
        emp_last_seen = emp.last_seen if emp else None
        status = compute_status(row, emp_last_seen, now)
        out.append(
            DeviceListItemOut(
                employee_id=row.employee_id,
                employee_name=emp.name if emp else None,
                machine_id=row.machine_id,
                hostname=row.hostname,
                agent_version=row.agent_version,
                status=status,
                last_seen=_resolve_last_seen(row, emp_last_seen),
                # last_upload：以 employees.last_seen 为锚（/activity 上报时机）
                # 区别于 last_seen（含 health 上报本身）。Phase 5.4 接入 window
                # sessions 后可改用 max(activity, window) 三者取大。
                last_upload=emp_last_seen,
                uptime_seconds=row.uptime_seconds,
                restart_count=row.restart_count,
                last_exit_reason=row.last_exit_reason,
                pending_events=row.pending_events,
                update_status=row.update_status or "idle",
                update_target_version=row.update_target_version,
            )
        )
    return out


def get_device_detail(db: Session, employee_id: int) -> DeviceDetailOut | None:
    """详情：按 employee_id 查 health 行。

    :return: ``None`` = 该员工还从未上报过 health
    """
    row = db.scalar(
        select(DeviceHealth).where(DeviceHealth.employee_id == employee_id)
    )
    if row is None:
        return None
    emp = db.get(Employee, employee_id)
    emp_last_seen = emp.last_seen if emp else None
    now = datetime.now(timezone.utc)
    status = compute_status(row, emp_last_seen, now)

    wd_obj: HealthWatchdogIn | None = None
    if row.watchdog_json:
        try:
            wd_obj = HealthWatchdogIn(**row.watchdog_json)
        except Exception:  # noqa: BLE001  老 schema / 字段缺失不应阻塞详情显示
            wd_obj = None

    return DeviceDetailOut(
        employee_id=row.employee_id,
        employee_name=emp.name if emp else None,
        machine_id=row.machine_id,
        hostname=row.hostname,
        agent_version=row.agent_version,
        status=status,
        last_seen=_resolve_last_seen(row, emp_last_seen),
        last_upload=emp_last_seen,
        uptime_seconds=row.uptime_seconds,
        restart_count=row.restart_count,
        last_exit_reason=row.last_exit_reason,
        pending_events=row.pending_events,
        update_status=row.update_status or "idle",
        update_target_version=row.update_target_version,
        process_started_at=row.process_started_at,
        first_start_at=row.first_start_at,
        last_start_at=row.last_start_at,
        last_exit_at=row.last_exit_at,
        last_report_at=row.last_report_at,
        watchdog=wd_obj,
        update_last_check_at=row.update_last_check_at,
        update_last_error=row.update_last_error,
    )

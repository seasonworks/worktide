"""工时 / Break 状态机服务（纯逻辑层）。

职责：根据 Telegram（或后台）触发的动作，驱动员工 work_state 状态机，并维护
shift_sessions / break_sessions / employee_status 三张表的一致性。

设计要点：
- 与活动状态（employees.status: online/idle/offline）正交，互不影响。
- 每个对外动作一个事务：成功 commit，异常 rollback（见 _transactional）。
- 幂等：重复同一动作不产生重复 session（靠 work_state / open session 守卫）。
- 并发安全：关闭 break/shift 前在事务内用带 `ended_at IS NULL` 条件的 UPDATE，
  靠 rowcount 判定是否本次真正关闭，避免“主动回座”与“超时清扫”重复关闭同一 break。
- 时间统一 UTC；从 SQLite 取回的 naive datetime 统一补 UTC（_aware）。

本模块不接 Telegram、不启动后台线程、不改现有上报/员工接口；仅读写第二阶段新增表。
"""
import functools
import logging
from collections.abc import Callable
from dataclasses import dataclass
from datetime import date as date_cls
from datetime import datetime, timedelta, timezone

from sqlalchemy import select, update
from sqlalchemy.orm import Session

from ..constants import (
    BREAK_TYPE_TO_WORK_STATE,
    BreakEndReason,
    BreakType,
    EmployeeStatus,
    WorkState,
)
from ..crud import effective_status
from ..models import (
    ActivityLog,
    BreakSession,
    Employee,
    EmployeeStatusRow,
    ShiftSession,
    utcnow,
)
from .settings_service import settings_service

logger = logging.getLogger(__name__)

# 处于休息中的 work_state 集合（用于暂停检测判定等）
_BREAK_STATES = {
    WorkState.break_meal.value,
    WorkState.break_toilet.value,
    WorkState.break_smoke.value,
}

# shift 结束原因（break 用 BreakEndReason；shift 暂只有正常下班一种）
_SHIFT_END_CLOCK_OUT = "clock_out"


@dataclass
class ActionResult:
    """状态机动作结果。

    changed：是否真正发生状态变更（幂等 noop 为 False）。
    work_state：动作后的 work_state。
    code：机器可读结果码（供日志/后台/未来 Telegram 文案映射）。
    message：人类可读说明（中文，未来可直接回 Telegram）。
    """

    changed: bool
    work_state: str
    code: str
    message: str


def _transactional(fn):
    """包裹对外动作：异常时回滚并重抛，避免脏事务残留。"""

    @functools.wraps(fn)
    def wrapper(db: Session, *args, **kwargs):
        try:
            return fn(db, *args, **kwargs)
        except Exception:
            db.rollback()
            logger.exception("work_state 动作失败：%s", fn.__name__)
            raise

    return wrapper


# ---------------------------------------------------------------------------
# 内部辅助
# ---------------------------------------------------------------------------


def _aware(dt: datetime | None) -> datetime | None:
    """SQLite 取回的 naive datetime 统一补 UTC tzinfo。"""
    if dt is not None and dt.tzinfo is None:
        return dt.replace(tzinfo=timezone.utc)
    return dt


def _activity_status(employee: Employee) -> str:
    """当前活动状态快照（含按 last_seen 计算的 offline）。"""
    return effective_status(employee).value


def _get_or_create_status(db: Session, employee_id: int) -> EmployeeStatusRow:
    status = db.get(EmployeeStatusRow, employee_id)
    if status is None:
        status = EmployeeStatusRow(
            employee_id=employee_id,
            work_state=WorkState.off_shift.value,
            state_since=utcnow(),
        )
        db.add(status)
        db.flush()
    return status


def _set_state(
    status: EmployeeStatusRow,
    work_state: str,
    now: datetime,
    active_break_id: int | None,
) -> None:
    status.work_state = work_state
    status.state_since = now
    status.active_break_id = active_break_id
    status.updated_at = now


def _find_open_shift(db: Session, employee_id: int) -> ShiftSession | None:
    return db.scalars(
        select(ShiftSession)
        .where(
            ShiftSession.employee_id == employee_id,
            ShiftSession.ended_at.is_(None),
        )
        .order_by(ShiftSession.started_at.desc())
    ).first()


def _find_open_break(db: Session, employee_id: int) -> BreakSession | None:
    return db.scalars(
        select(BreakSession)
        .where(
            BreakSession.employee_id == employee_id,
            BreakSession.ended_at.is_(None),
        )
        .order_by(BreakSession.started_at.desc())
    ).first()


def _open_shift(db: Session, employee_id: int, now: datetime) -> ShiftSession:
    shift = ShiftSession(employee_id=employee_id, started_at=now)
    db.add(shift)
    db.flush()  # 取自增 id
    return shift


def _close_shift(db: Session, shift: ShiftSession, reason: str, now: datetime) -> bool:
    """关闭 shift（带 ended_at IS NULL 守卫，幂等/并发安全）。"""
    started = _aware(shift.started_at)
    duration = max(0, int((now - started).total_seconds()))
    result = db.execute(
        update(ShiftSession)
        .where(ShiftSession.id == shift.id, ShiftSession.ended_at.is_(None))
        .values(ended_at=now, end_reason=reason, duration_seconds=duration)
    )
    closed = result.rowcount == 1
    if closed:
        db.expire(shift)  # 让 ORM 实例与库一致
    return closed


def _open_break(
    db: Session,
    employee: Employee,
    shift: ShiftSession | None,
    break_type: BreakType,
    now: datetime,
) -> BreakSession:
    planned = settings_service.get_break_max(break_type)  # 快照当时上限
    brk = BreakSession(
        employee_id=employee.id,
        shift_id=shift.id if shift is not None else None,
        break_type=break_type.value,
        started_at=now,
        planned_max_seconds=planned,
        started_from_activity_status=_activity_status(employee),
    )
    db.add(brk)
    db.flush()  # 取自增 id（供 active_break_id 引用）
    return brk


def _end_break(
    db: Session,
    brk: BreakSession,
    reason: BreakEndReason,
    now: datetime,
    auto_ended: bool = False,
) -> bool:
    """关闭 break（带 ended_at IS NULL 守卫）。

    返回是否本次真正关闭：rowcount==0 表示已被其它路径关闭（如回座 vs 超时竞态），
    调用方据此避免重复改状态。
    """
    started = _aware(brk.started_at)
    duration = max(0, int((now - started).total_seconds()))
    result = db.execute(
        update(BreakSession)
        .where(BreakSession.id == brk.id, BreakSession.ended_at.is_(None))
        .values(
            ended_at=now,
            end_reason=reason.value,
            auto_ended=auto_ended,
            duration_seconds=duration,
        )
    )
    closed = result.rowcount == 1
    if closed:
        db.expire(brk)
    return closed


# ---------------------------------------------------------------------------
# 对外动作
# ---------------------------------------------------------------------------


@_transactional
def clock_in(db: Session, employee_id: int) -> ActionResult:
    """上班打卡：off_shift -> working，开 shift。working/break_* 时幂等。"""
    employee = db.get(Employee, employee_id)
    if employee is None:
        return ActionResult(False, WorkState.off_shift.value, "unknown_employee", "未知员工")

    status = _get_or_create_status(db, employee_id)
    if status.work_state != WorkState.off_shift.value:
        db.rollback()  # 仅可能创建了 status 行，无业务变更则回滚
        return ActionResult(
            False, status.work_state, "already_on_shift", "你已经在上班/休息状态，无需重复上班"
        )

    now = utcnow()
    _open_shift(db, employee_id, now)
    _set_state(status, WorkState.working.value, now, active_break_id=None)
    db.commit()
    logger.info("上班打卡 employee_id=%s", employee_id)
    return ActionResult(True, WorkState.working.value, "clock_in", "已上班打卡，开始工作")


@_transactional
def clock_out(db: Session, employee_id: int) -> ActionResult:
    """下班打卡：若在 break 先以 shift_end 结束，再关闭 shift，work_state -> off_shift。

    off_shift 时幂等 noop。
    """
    employee = db.get(Employee, employee_id)
    if employee is None:
        return ActionResult(False, WorkState.off_shift.value, "unknown_employee", "未知员工")

    status = _get_or_create_status(db, employee_id)
    if status.work_state == WorkState.off_shift.value:
        db.rollback()
        return ActionResult(
            False, WorkState.off_shift.value, "already_off_shift", "你已经是下班状态"
        )

    now = utcnow()
    open_break = _find_open_break(db, employee_id)
    if open_break is not None:
        _end_break(db, open_break, BreakEndReason.shift_end, now, auto_ended=False)

    open_shift = _find_open_shift(db, employee_id)
    if open_shift is not None:
        _close_shift(db, open_shift, _SHIFT_END_CLOCK_OUT, now)

    _set_state(status, WorkState.off_shift.value, now, active_break_id=None)
    db.commit()
    logger.info("下班打卡 employee_id=%s", employee_id)
    return ActionResult(True, WorkState.off_shift.value, "clock_out", "已下班打卡，辛苦了")


@_transactional
def start_break(
    db: Session,
    employee_id: int,
    break_type,
    on_switch: Callable[[int, str, str, str], None] | None = None,
) -> ActionResult:
    """开始休息：working -> break_x。

    - off_shift：不允许（需先上班）。
    - 同型 break：幂等 noop。
    - 异型 break：先以 switched 结束旧 break，再开新 break。

    Phase 6.5A · GS-7B 钩子：异型切换（switched=True）且 commit 成功后，
    调用 on_switch(employee_id, employee_name, from_break_type, to_break_type)。
    回调失败仅记日志，不回滚业务（业务已 commit）。
    """
    bt = break_type if isinstance(break_type, BreakType) else BreakType(break_type)
    target_state = BREAK_TYPE_TO_WORK_STATE[bt].value

    employee = db.get(Employee, employee_id)
    if employee is None:
        return ActionResult(False, WorkState.off_shift.value, "unknown_employee", "未知员工")

    status = _get_or_create_status(db, employee_id)
    if status.work_state == WorkState.off_shift.value:
        db.rollback()
        return ActionResult(
            False, WorkState.off_shift.value, "not_on_shift", "请先上班再开始休息"
        )

    now = utcnow()
    switched = False
    prev_break_type: str | None = None
    open_break = _find_open_break(db, employee_id)
    if open_break is not None:
        if open_break.break_type == bt.value:
            db.rollback()
            return ActionResult(
                False, status.work_state, "break_already_active", "你已经在该休息状态中"
            )
        # 异型：先结束旧 break（switched），再开新 break
        prev_break_type = open_break.break_type   # 留给 on_switch 回调
        _end_break(db, open_break, BreakEndReason.switched, now, auto_ended=False)
        switched = True

    shift = _find_open_shift(db, employee_id)
    brk = _open_break(db, employee, shift, bt, now)
    _set_state(status, target_state, now, active_break_id=brk.id)
    db.commit()

    if switched:
        logger.info("切换休息 employee_id=%s -> %s", employee_id, bt.value)
        # GS-7B hook：业务已 commit，回调异常不应回滚
        if on_switch is not None and prev_break_type is not None:
            try:
                on_switch(employee_id, employee.name, prev_break_type, bt.value)
            except Exception:  # noqa: BLE001
                logger.exception("on_switch 回调失败 emp=%s", employee_id)
        return ActionResult(True, target_state, "break_switched", f"已切换休息：{bt.value}")
    logger.info("开始休息 employee_id=%s type=%s", employee_id, bt.value)
    return ActionResult(True, target_state, "break_started", f"已开始休息：{bt.value}")


@_transactional
def return_to_work(db: Session, employee_id: int) -> ActionResult:
    """回座：break_* -> working，以 manual_return 结束当前 break。非 break 幂等 noop。"""
    employee = db.get(Employee, employee_id)
    if employee is None:
        return ActionResult(False, WorkState.off_shift.value, "unknown_employee", "未知员工")

    status = _get_or_create_status(db, employee_id)
    open_break = _find_open_break(db, employee_id)
    if open_break is None:
        db.rollback()
        return ActionResult(False, status.work_state, "not_on_break", "你当前不在休息中")

    now = utcnow()
    closed = _end_break(db, open_break, BreakEndReason.manual_return, now)
    _set_state(status, WorkState.working.value, now, active_break_id=None)
    db.commit()

    if not closed:
        # 竞态：该 break 已被超时清扫等关闭，状态已是 working，此处仅对齐
        logger.info("回座时 break 已被关闭 employee_id=%s", employee_id)
        return ActionResult(False, WorkState.working.value, "already_returned", "你已经在工作中")
    logger.info("回座 employee_id=%s", employee_id)
    return ActionResult(True, WorkState.working.value, "returned", "已回座，继续工作")


@_transactional
def force_resume_working(db: Session, employee_id: int) -> ActionResult:
    """后台强制恢复在岗（预留：本轮不暴露 API）。

    结束当前 break（end_reason=admin_force, auto_ended=True），work_state -> working。
    - off_shift：不动（working 需有 open shift，强制建态会破坏不变量）。
    - working 且无 break：幂等 noop。
    """
    employee = db.get(Employee, employee_id)
    if employee is None:
        return ActionResult(False, WorkState.off_shift.value, "unknown_employee", "未知员工")

    status = _get_or_create_status(db, employee_id)
    if status.work_state == WorkState.off_shift.value:
        db.rollback()
        return ActionResult(
            False, WorkState.off_shift.value, "not_on_shift", "员工处于下班状态，无法强制在岗"
        )

    now = utcnow()
    open_break = _find_open_break(db, employee_id)
    if open_break is None:
        if status.work_state == WorkState.working.value:
            db.rollback()
            return ActionResult(False, WorkState.working.value, "already_working", "员工已在工作中")
        # work_state 标记为 break 但无 open break：修正为 working
        _set_state(status, WorkState.working.value, now, active_break_id=None)
        db.commit()
        logger.info("强制对齐为在岗 employee_id=%s", employee_id)
        return ActionResult(True, WorkState.working.value, "force_resume", "已强制恢复在岗")

    _end_break(db, open_break, BreakEndReason.admin_force, now, auto_ended=True)
    _set_state(status, WorkState.working.value, now, active_break_id=None)
    db.commit()
    logger.info("强制恢复在岗 employee_id=%s", employee_id)
    return ActionResult(True, WorkState.working.value, "force_resume", "已强制恢复在岗")


def expire_overdue_breaks(
    db: Session,
    on_timeout: Callable[[int, str, str, int], None] | None = None,
) -> list[ActionResult]:
    """扫描所有未结束 break，超过 planned_max_seconds 的强制结束。

    每个超时 break 单独成事务（commit/rollback 互不影响），便于后台 sweeper 调用。
    结束原因 timeout、auto_ended=True，并将对应员工 work_state 恢复为 working。
    返回本次实际处理（成功关闭）的结果列表。

    Phase 6.5A · GS-3 钩子：成功 commit 后，调用
    on_timeout(employee_id, employee_name, break_type, duration_seconds)。
    回调失败仅记日志，不影响业务（事务已落地）。
    """
    now = utcnow()
    open_breaks = db.scalars(
        select(BreakSession).where(BreakSession.ended_at.is_(None))
    ).all()

    results: list[ActionResult] = []
    for brk in open_breaks:
        started = _aware(brk.started_at)
        if (now - started).total_seconds() <= brk.planned_max_seconds:
            continue
        # GS-3 回调参数预先取出（_end_break 会 expire ORM 对象）
        emp_id_local = brk.employee_id
        break_type_local = brk.break_type
        duration_local = max(0, int((now - started).total_seconds()))
        break_id_for_log = brk.id
        try:
            closed = _end_break(db, brk, BreakEndReason.timeout, now, auto_ended=True)
            if closed:
                status = _get_or_create_status(db, emp_id_local)
                # 仅当员工仍处于休息态时才恢复（可能已下班/回座）
                if status.work_state in _BREAK_STATES:
                    _set_state(status, WorkState.working.value, now, active_break_id=None)
            db.commit()
            if closed:
                logger.info(
                    "break 超时自动结束 employee_id=%s break_id=%s type=%s",
                    emp_id_local,
                    break_id_for_log,
                    break_type_local,
                )
                results.append(
                    ActionResult(
                        True,
                        WorkState.working.value,
                        "break_timeout",
                        f"休息超时，已自动恢复在岗：{break_type_local}",
                    )
                )
                # GS-3 hook：业务已 commit，回调异常不阻断后续 break 的处理
                if on_timeout is not None:
                    try:
                        employee = db.get(Employee, emp_id_local)
                        emp_name = employee.name if employee is not None else f"#{emp_id_local}"
                        on_timeout(emp_id_local, emp_name, break_type_local, duration_local)
                    except Exception:  # noqa: BLE001
                        logger.exception(
                            "on_timeout 回调失败 emp=%s break_id=%s",
                            emp_id_local, break_id_for_log,
                        )
        except Exception:
            db.rollback()
            logger.exception("处理超时 break 失败 break_id=%s", break_id_for_log)
    return results


@dataclass
class OverdueBreak:
    """GS-13 · 一条超时但仍未结束的 break 快照（只读，不改库）。"""

    break_id: int
    employee_id: int
    employee_name: str
    break_type: str
    duration_seconds: int


def find_overdue_breaks(db: Session) -> list[OverdueBreak]:
    """GS-13 · OFF 模式专用：扫描所有超过 planned_max_seconds 的未结束 break，
    **只读返回，不结束 break、不改 work_state**。

    与 expire_overdue_breaks 的区别：后者会自动回座（关闭 break + 恢复 working），
    本函数仅供 sweeper 在 auto_return_after_break_timeout=False 时发"请手动返回"通知。
    去重（同一 break 只通知一次）由调用方 sweeper 负责。
    """
    now = utcnow()
    open_breaks = db.scalars(
        select(BreakSession).where(BreakSession.ended_at.is_(None))
    ).all()
    overdue: list[OverdueBreak] = []
    for brk in open_breaks:
        started = _aware(brk.started_at)
        if (now - started).total_seconds() <= brk.planned_max_seconds:
            continue
        employee = db.get(Employee, brk.employee_id)
        overdue.append(
            OverdueBreak(
                break_id=brk.id,
                employee_id=brk.employee_id,
                employee_name=employee.name if employee is not None else f"#{brk.employee_id}",
                break_type=brk.break_type,
                duration_seconds=max(0, int((now - started).total_seconds())),
            )
        )
    return overdue


def is_detection_paused(db: Session, employee_id: int) -> bool:
    """是否应暂停该员工的 idle/offline 告警：处于 break_* 且开关开启时为 True。

    注意：仅判定 break_*（break_meal/break_toilet/break_smoke）+ 受
    `break_pauses_detection` 设置控制。**off_shift 不在判定范围**，由
    `is_off_shift()` 单独覆盖，是考勤层面的硬规则、不受 UI 开关影响。
    """
    status = db.get(EmployeeStatusRow, employee_id)
    if status is None or status.work_state not in _BREAK_STATES:
        return False
    return bool(settings_service.get("break_pauses_detection"))


def is_off_shift(db: Session, employee_id: int) -> bool:
    """是否已下班（off_shift）。off_shift 员工不应参与 idle_enter/idle_exit 通知。

    Phase 6.5A · BUG-009 修复：与 `is_detection_paused` 是两条独立 gate，
    activity.report 一并判定。off_shift 是考勤外的硬排除（与 settings 无关），
    例如已下班 AFK 不应再触发 🟡 挂机通知。
    """
    status = db.get(EmployeeStatusRow, employee_id)
    return status is not None and status.work_state == WorkState.off_shift.value


# ---------------------------------------------------------------------------
# 查询 / 聚合（只读，供后台 API；不改库，duration 实时计算）
# ---------------------------------------------------------------------------


def _elapsed_seconds(start: datetime, now: datetime) -> int:
    return max(0, int((now - _aware(start)).total_seconds()))


def _break_duration(brk: BreakSession, now: datetime) -> int:
    """已结束取落库 duration，进行中实时计算。"""
    if brk.ended_at is not None and brk.duration_seconds is not None:
        return brk.duration_seconds
    return _elapsed_seconds(brk.started_at, now)


def _shift_duration(shift: ShiftSession, now: datetime) -> int:
    if shift.ended_at is not None and shift.duration_seconds is not None:
        return shift.duration_seconds
    return _elapsed_seconds(shift.started_at, now)


def _day_bounds(day: date_cls) -> tuple[datetime, datetime]:
    """某 UTC 日的 [00:00, 次日00:00) 边界。"""
    start = datetime(day.year, day.month, day.day, tzinfo=timezone.utc)
    return start, start + timedelta(days=1)


def list_work_status(db: Session, include_inactive: bool = False) -> list[dict]:
    """后台首页数据：每个员工的活动状态 + work_state + 实时 break/shift 秒数。

    employee_status 缺失时视为 off_shift。break/shift 进行中时长用 now-start 实时计算。
    默认仅返回在职员工；离职员工的历史 break/shift 仍可通过 /work/employees/{id}/* 查询。
    """
    now = utcnow()
    emp_stmt = select(Employee).order_by(Employee.name)
    if not include_inactive:
        emp_stmt = emp_stmt.where(Employee.is_active.is_(True))
    employees = db.scalars(emp_stmt).all()
    status_map = {s.employee_id: s for s in db.scalars(select(EmployeeStatusRow)).all()}

    open_shifts: dict[int, ShiftSession] = {}
    for sh in db.scalars(
        select(ShiftSession).where(ShiftSession.ended_at.is_(None))
    ).all():
        cur = open_shifts.get(sh.employee_id)
        if cur is None or _aware(sh.started_at) > _aware(cur.started_at):
            open_shifts[sh.employee_id] = sh

    open_brks: dict[int, BreakSession] = {}
    for bk in db.scalars(
        select(BreakSession).where(BreakSession.ended_at.is_(None))
    ).all():
        cur = open_brks.get(bk.employee_id)
        if cur is None or _aware(bk.started_at) > _aware(cur.started_at):
            open_brks[bk.employee_id] = bk

    out: list[dict] = []
    for emp in employees:
        st = status_map.get(emp.id)
        shift = open_shifts.get(emp.id)
        brk = open_brks.get(emp.id)
        out.append(
            {
                "employee_id": emp.id,
                "name": emp.name,
                "hostname": emp.hostname,
                "activity_status": effective_status(emp, now).value,
                "work_state": st.work_state if st else WorkState.off_shift.value,
                "break_type": brk.break_type if brk else None,
                "current_break_seconds": _elapsed_seconds(brk.started_at, now) if brk else 0,
                "current_shift_seconds": _elapsed_seconds(shift.started_at, now) if shift else 0,
            }
        )
    return out


def list_employee_breaks(
    db: Session,
    employee_id: int,
    limit: int = 100,
    break_type=None,
    day: date_cls | None = None,
) -> list[BreakSession]:
    """某员工的 break 历史，时间倒序。可选 break_type / 单日过滤。"""
    stmt = select(BreakSession).where(BreakSession.employee_id == employee_id)
    if break_type is not None:
        bt = break_type if isinstance(break_type, BreakType) else BreakType(break_type)
        stmt = stmt.where(BreakSession.break_type == bt.value)
    if day is not None:
        start, end = _day_bounds(day)
        stmt = stmt.where(
            BreakSession.started_at >= start, BreakSession.started_at < end
        )
    stmt = stmt.order_by(BreakSession.started_at.desc()).limit(limit)
    return list(db.scalars(stmt))


def list_employee_shifts(
    db: Session, employee_id: int, limit: int = 100
) -> list[ShiftSession]:
    """某员工的 shift 历史，时间倒序。"""
    stmt = (
        select(ShiftSession)
        .where(ShiftSession.employee_id == employee_id)
        .order_by(ShiftSession.started_at.desc())
        .limit(limit)
    )
    return list(db.scalars(stmt))


def daily_stats(
    db: Session, day: date_cls, include_inactive: bool = False
) -> list[dict]:
    """按员工聚合某 UTC 日的工时与各类 break 次数/时长。

    gross_shift_seconds：当日开始的 shift 总时长（进行中实时计）。
    break_seconds：当日开始的 break 总时长。net_work_seconds = gross - break（下限 0）。
    默认仅在职员工；离职员工历史不参与默认统计（与"不再参与统计"语义一致）。
    """
    now = utcnow()
    start, end = _day_bounds(day)
    emp_stmt = select(Employee).order_by(Employee.name)
    if not include_inactive:
        emp_stmt = emp_stmt.where(Employee.is_active.is_(True))
    employees = db.scalars(emp_stmt).all()

    per: dict[int, dict] = {
        e.id: {
            "toilet_count": 0,
            "toilet_seconds": 0,
            "smoke_count": 0,
            "smoke_seconds": 0,
            "meal_count": 0,
            "meal_seconds": 0,
            "gross_shift_seconds": 0,
            "break_seconds": 0,
        }
        for e in employees
    }

    for bk in db.scalars(
        select(BreakSession).where(
            BreakSession.started_at >= start, BreakSession.started_at < end
        )
    ).all():
        d = per.get(bk.employee_id)
        if d is None or f"{bk.break_type}_count" not in d:
            continue
        secs = _break_duration(bk, now)
        d[f"{bk.break_type}_count"] += 1
        d[f"{bk.break_type}_seconds"] += secs
        d["break_seconds"] += secs

    for sh in db.scalars(
        select(ShiftSession).where(
            ShiftSession.started_at >= start, ShiftSession.started_at < end
        )
    ).all():
        d = per.get(sh.employee_id)
        if d is None:
            continue
        d["gross_shift_seconds"] += _shift_duration(sh, now)

    out: list[dict] = []
    for emp in employees:
        d = per[emp.id]
        d["net_work_seconds"] = max(0, d["gross_shift_seconds"] - d["break_seconds"])
        out.append({"employee_id": emp.id, "name": emp.name, "date": day.isoformat(), **d})
    return out


# 单个 idle 段最多计 _IDLE_GAP_CAP_SECONDS，避免离线/上报中断的空洞被算成挂机
_IDLE_GAP_CAP_SECONDS = 90


def _late_seconds_for(started_at: datetime) -> int:
    """班次 clock-in 相对设置的上班时刻迟到多少秒（早到/准时/无配置 → 0）。

    按 reminders_timezone_offset_hours 把 started_at 换成本地墙钟，与 clock_in_reminder_time
    比较。跨午夜班次（如 22:00）：本地时间比上班时刻早 12h 以上 → 视为上班时刻在昨天。
    """
    try:
        shift_hhmm = str(settings_service.get("clock_in_reminder_time") or "")
        offset = int(settings_service.get("reminders_timezone_offset_hours"))
        sh, sm = (int(x) for x in shift_hhmm.split(":", 1))
    except (ValueError, TypeError, AttributeError):
        return 0
    local = _aware(started_at) + timedelta(hours=offset)
    shift_start = local.replace(hour=sh, minute=sm, second=0, microsecond=0)
    late = (local - shift_start).total_seconds()
    if late < -12 * 3600:        # 跨午夜：上班时刻其实是昨天
        late += 24 * 3600
    return max(0, int(late))


def _cumulative_idle_seconds(
    db: Session, employee_id: int, start: datetime, end: datetime
) -> int:
    """窗口内累计挂机秒数。

    挂机检测有 idle_threshold（默认 900s）前导期：客户端每 ~30s 上报一次 OS 空闲秒数，
    只有累计 ≥ 阈值才会被标成 idle，之前都记成 online。故每段挂机由两部分组成：
      1. 检测前导期 —— 进入该段的「首条 idle 上报」的 idle_seconds，正是这一刻已空闲
         的真实时长（含被记成 online 的那段阈值期）。仅在进入新一段 idle 时补一次，
         上限不超过窗口起点到该刻（避免把开班前的空闲算进来）。Bug-012 同因。
      2. 持续期 —— 每条 idle 上报到下一条的间隔，单段封顶 _IDLE_GAP_CAP_SECONDS，
         避免离线/上报中断造成的大空洞被误算成挂机。
    数据来自每 ~30s 一次的 activity_logs，故为近似值（±一个上报周期）。
    """
    logs = db.scalars(
        select(ActivityLog)
        .where(
            ActivityLog.employee_id == employee_id,
            ActivityLog.reported_at >= start,
            ActivityLog.reported_at <= end,
        )
        .order_by(ActivityLog.reported_at.asc())
    ).all()
    total = 0.0
    prev_idle = False
    for i, lg in enumerate(logs):
        if lg.status != EmployeeStatus.idle.value:
            prev_idle = False
            continue
        cur = _aware(lg.reported_at)
        if not prev_idle:
            # 新一段 idle 起点：补检测前导期（首条 idle 上报的真实已空闲时长）
            lead_in = min(float(max(0, lg.idle_seconds or 0)), (cur - start).total_seconds())
            total += max(0.0, lead_in)
        nxt = _aware(logs[i + 1].reported_at) if i + 1 < len(logs) else end
        gap = (nxt - cur).total_seconds()
        total += min(max(0.0, gap), _IDLE_GAP_CAP_SECONDS)
        prev_idle = True
    return int(total)


def shift_stats(db: Session, employee_id: int) -> dict | None:
    """当前（进行中）或最近一次已结束班次的统计。该员工无任何班次 → None。

    含：班次起止 + is_current、净工时/在岗/break 总时长、三类 break 次数与时长、
    迟到秒数（相对设置的上班时刻）、累计挂机秒数（窗口内 idle 近似累加）。
    """
    now = utcnow()
    shift = _find_open_shift(db, employee_id)
    is_current = shift is not None
    if shift is None:
        shift = db.scalars(
            select(ShiftSession)
            .where(ShiftSession.employee_id == employee_id)
            .order_by(ShiftSession.started_at.desc())
        ).first()
    if shift is None:
        return None

    end = _aware(shift.ended_at) if shift.ended_at is not None else now

    d = {
        "toilet_count": 0, "toilet_seconds": 0,
        "smoke_count": 0, "smoke_seconds": 0,
        "meal_count": 0, "meal_seconds": 0,
        "break_seconds": 0,
    }
    for bk in db.scalars(
        select(BreakSession).where(
            BreakSession.employee_id == employee_id,
            BreakSession.started_at >= shift.started_at,
            BreakSession.started_at <= end,
        )
    ).all():
        if f"{bk.break_type}_count" not in d:
            continue
        secs = _break_duration(bk, now)
        d[f"{bk.break_type}_count"] += 1
        d[f"{bk.break_type}_seconds"] += secs
        d["break_seconds"] += secs

    gross = _shift_duration(shift, now)
    return {
        "employee_id": employee_id,
        "shift_started_at": _aware(shift.started_at),
        "shift_ended_at": _aware(shift.ended_at) if shift.ended_at is not None else None,
        "is_current": is_current,
        "gross_shift_seconds": gross,
        "break_seconds": d["break_seconds"],
        "net_work_seconds": max(0, gross - d["break_seconds"]),
        "idle_seconds_total": _cumulative_idle_seconds(db, employee_id, _aware(shift.started_at), end),
        "late_seconds": _late_seconds_for(shift.started_at),
        "toilet_count": d["toilet_count"], "toilet_seconds": d["toilet_seconds"],
        "smoke_count": d["smoke_count"], "smoke_seconds": d["smoke_seconds"],
        "meal_count": d["meal_count"], "meal_seconds": d["meal_seconds"],
    }

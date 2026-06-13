import logging
from datetime import datetime, timezone

from sqlalchemy import delete as sa_delete, select, update as sa_update
from sqlalchemy.orm import Session, joinedload

from . import models
from .config import settings
from .constants import EmployeeStatus
from .schemas import ActivityReport

logger = logging.getLogger(__name__)


class CloneSuspectBlocked(Exception):
    """Phase 6.0D · 设备身份硬化（strict 模式）专用领域异常。

    同 machine_id 但 hw_fingerprint 不匹配时抛出；router 层捕获并转 HTTP 409。
    Loose 模式（默认）不抛此异常，只在 employees 表里递增 clone_suspect_count。
    """

    def __init__(self, employee_id: int, machine_id: str,
                 expected_fp: str, got_fp: str) -> None:
        super().__init__(
            f"machine_id={machine_id} 已绑定 hw_fingerprint={expected_fp[:8]}…，"
            f"本次上报 hw_fingerprint={got_fp[:8]}… 不一致（emp={employee_id}）"
        )
        self.employee_id = employee_id
        self.machine_id = machine_id
        self.expected_fp = expected_fp
        self.got_fp = got_fp


def get_employee(db: Session, employee_id: int) -> models.Employee | None:
    return db.get(models.Employee, employee_id)


def update_employee(
    db: Session, employee_id: int, name: str
) -> models.Employee | None:
    """后台改名：仅设置员工姓名。不存在返回 None。

    与 record_report 上报流程正交：客户端 employee_name 为空时上报 name=None，
    不会覆盖此处写入的姓名（详见 record_report 中 `if report.name:` 守卫）。
    """
    employee = db.get(models.Employee, employee_id)
    if employee is None:
        return None
    employee.name = name
    db.commit()
    db.refresh(employee)
    return employee


def get_employee_by_machine(db: Session, machine_id: str) -> models.Employee | None:
    return db.scalar(
        select(models.Employee).where(models.Employee.machine_id == machine_id)
    )


def list_employees(
    db: Session, include_inactive: bool = False
) -> list[models.Employee]:
    """员工列表。默认仅在职；离职员工历史保留，需显式 include_inactive=True 才返回。"""
    stmt = select(models.Employee).order_by(models.Employee.name)
    if not include_inactive:
        stmt = stmt.where(models.Employee.is_active.is_(True))
    return list(db.scalars(stmt))


def archive_employee(db: Session, employee_id: int) -> models.Employee | None:
    """软删除：标记员工离职。所有关联表保持不变；不级联删除。

    runtime state（shift/break）由路由层在调用此函数前用 work_state.clock_out 收口。
    """
    employee = db.get(models.Employee, employee_id)
    if employee is None:
        return None
    employee.is_active = False
    employee.deleted_at = datetime.now(timezone.utc)
    db.commit()
    db.refresh(employee)
    return employee


def restore_employee(db: Session, employee_id: int) -> models.Employee | None:
    """恢复员工。is_active=True、deleted_at=None；不自动 clock_in，从 off_shift 重启。"""
    employee = db.get(models.Employee, employee_id)
    if employee is None:
        return None
    employee.is_active = True
    employee.deleted_at = None
    db.commit()
    db.refresh(employee)
    return employee


def list_logs(
    db: Session, employee_id: int | None = None, limit: int = 100
) -> list[models.ActivityLog]:
    stmt = (
        select(models.ActivityLog)
        .order_by(models.ActivityLog.reported_at.desc())
        .limit(limit)
    )
    if employee_id is not None:
        stmt = stmt.where(models.ActivityLog.employee_id == employee_id)
    return list(db.scalars(stmt))


def list_recent_logs(
    db: Session, limit: int = 100, employee_id: int | None = None
) -> list[models.ActivityLog]:
    """最近的活动记录，预加载员工（取姓名/电脑名），避免 N+1 查询。"""
    stmt = (
        select(models.ActivityLog)
        .options(joinedload(models.ActivityLog.employee))
        .order_by(models.ActivityLog.reported_at.desc())
        .limit(limit)
    )
    if employee_id is not None:
        stmt = stmt.where(models.ActivityLog.employee_id == employee_id)
    return list(db.scalars(stmt))


def delete_logs_older_than(
    db: Session, cutoff: datetime, batch_size: int = 5000
) -> int:
    """分批删除 reported_at < cutoff 的历史记录，返回删除总行数。

    每批单独提交，避免一次性大删长时间持写锁（配合 WAL 防锁表）。
    """
    total = 0
    while True:
        ids = db.scalars(
            select(models.ActivityLog.id)
            .where(models.ActivityLog.reported_at < cutoff)
            .limit(batch_size)
        ).all()
        if not ids:
            break
        db.execute(sa_delete(models.ActivityLog).where(models.ActivityLog.id.in_(ids)))
        db.commit()
        total += len(ids)
        if len(ids) < batch_size:
            break
    return total


def _resolve_reported_status(idle_seconds: int) -> EmployeeStatus:
    """根据空闲秒数判定上报时刻的状态（不含 offline）。"""
    if idle_seconds >= settings.idle_threshold_seconds:
        return EmployeeStatus.idle
    return EmployeeStatus.online


def _relink_device_health_machine_id(
    db: Session, old_machine_id: str, new_machine_id: str
) -> int:
    """Phase 6.0D · 设备身份迁移辅助（6.0D-S.1 hotfix：先删后改）。

    device_health 也按 machine_id 唯一索引，迁移 employees.machine_id 后必须把
    device_health 同一行的 machine_id 同步改写，否则下次 health 上报会创建
    第二个 device_health 行（孤儿）。

    **6.0D-S.1 hotfix**：之前的实现直接 UPDATE，在如下竞态下会触发 UNIQUE
    约束 500：
        1. 升级后的 0.6.0 Agent 启动，HealthReporter 线程先 POST /agent/health →
           upsert_health INSERT 新行 (mid=NEW_UUID4, employee_id=NULL)
        2. Reporter 线程随后 POST /activity/report → _try_migrate_legacy → 试图
           UPDATE device_health SET mid=NEW_UUID4 WHERE mid=OLD_MID
        3. 上一步发现 mid=NEW_UUID4 已有一行（步骤 1 的孤儿）→ UNIQUE 爆 500
        4. 客户端持续重试 → 持续 500 → 直至 Agent 进程重启丢失 legacy →
           走 CASE C 创建新员工行 → 历史断裂
    修复：UPDATE 之前先 DELETE 任何已存在的 NEW_MID 行（孤儿），把
    NEW_MID 的位置让给 OLD_MID 的真身。OLD_MID 行带有完整的 0.5.4 期
    history（first_start_at, restart_count, watchdog_json 等）。

    返回受影响（被 rename 的）行数（0 或 1）。
    """
    # 6.0D-S.1: 先吃掉抢跑插入的孤儿（如有），再 rename，避免 UNIQUE 500
    db.execute(
        sa_delete(models.DeviceHealth)
        .where(models.DeviceHealth.machine_id == new_machine_id)
    )
    result = db.execute(
        sa_update(models.DeviceHealth)
        .where(models.DeviceHealth.machine_id == old_machine_id)
        .values(machine_id=new_machine_id)
    )
    return int(result.rowcount or 0)


def _apply_hw_fingerprint(
    employee: models.Employee, report: ActivityReport, now: datetime
) -> None:
    """Phase 6.0D · 同 machine_id 的 hw_fingerprint 应用与克隆嫌疑标记。

    分三种情况：
      a) report 没带 hw_fp（≤0.5.x client）→ 不动
      b) employee 还没记 hw_fp → 首次记录
      c) employee 已有 hw_fp 且与本次一致 → 续保
      c') employee 已有 hw_fp 但与本次不一致 → 递增 clone_suspect_count
           + 写 clone_suspect_last_at + WARN 日志；strict 模式下由 record_report
           抛 CloneSuspectBlocked。
    """
    if not report.hw_fingerprint:
        return  # case a
    if not employee.hw_fingerprint:
        # case b
        employee.hw_fingerprint = report.hw_fingerprint
        employee.hw_fingerprint_first_seen = now
        return
    if employee.hw_fingerprint == report.hw_fingerprint:
        return  # case c
    # case c': clone suspect
    employee.clone_suspect_count = (employee.clone_suspect_count or 0) + 1
    employee.clone_suspect_last_at = now
    logger.warning(
        "clone-suspect: emp_id=%s machine_id=%s expected_fp=%s… got_fp=%s… "
        "suspect_count=%s strict_mode=%s",
        employee.id,
        report.machine_id,
        (employee.hw_fingerprint or "")[:8],
        report.hw_fingerprint[:8],
        employee.clone_suspect_count,
        settings.device_identity_strict_mode,
    )


def _try_migrate_legacy(
    db: Session, report: ActivityReport, now: datetime
) -> models.Employee | None:
    """Phase 6.0D · 0.6.0 首启迁移：旧 MachineGuid → 新 UUID4。

    入口条件：report.legacy_machine_id 非空 且 report.machine_id 在 employees
    中找不到。本函数查询 legacy 行，根据 hw_fingerprint 决定是迁移还是当作
    克隆分裂（返回 None 让 record_report 走新建分支）。

    迁移成功 → 返回 employee（machine_id 已被改写为新 UUID4）。
    legacy 查不到 / hw 不匹配 → 返回 None。
    """
    if not report.legacy_machine_id:
        return None
    legacy_emp = get_employee_by_machine(db, report.legacy_machine_id)
    if legacy_emp is None:
        return None

    # hw_match 规则：
    #   legacy_emp.hw_fingerprint 为空 → 之前没采集过，本次写入即可，视为匹配
    #   本次 report.hw_fingerprint 为空 → 无从校验，宽松处理：当作匹配（迁移）
    #   两者都有且相等 → 匹配
    hw_match = (
        not legacy_emp.hw_fingerprint
        or not report.hw_fingerprint
        or legacy_emp.hw_fingerprint == report.hw_fingerprint
    )
    if not hw_match:
        # hw 不一致 → 真克隆，不迁移；调用方走新建分支让这台机器拿到独立 employee 行
        logger.warning(
            "identity-split: legacy_mid=%s 与新 hw_fp 不一致 → 拒绝迁移，"
            "分裂为新 employee (legacy_emp_id=%s)",
            report.legacy_machine_id,
            legacy_emp.id,
        )
        return None

    # 执行迁移：改写 employees.machine_id + 同步 device_health + 写审计字段
    old_mid = legacy_emp.machine_id
    legacy_emp.machine_id = report.machine_id
    legacy_emp.legacy_machine_id = report.legacy_machine_id
    if report.hw_fingerprint and not legacy_emp.hw_fingerprint:
        legacy_emp.hw_fingerprint = report.hw_fingerprint
        legacy_emp.hw_fingerprint_first_seen = now
    relinked = _relink_device_health_machine_id(db, old_mid, report.machine_id)
    db.flush()
    logger.info(
        "identity-migrate: emp_id=%s machine_id %s → %s (legacy 已存为审计字段；"
        "device_health 同步 %s 行)",
        legacy_emp.id,
        old_mid,
        report.machine_id,
        relinked,
    )
    return legacy_emp


def record_report(
    db: Session, report: ActivityReport
) -> tuple[models.Employee, models.ActivityLog]:
    """处理一次客户端上报：自动注册员工、更新状态、落历史日志。

    Phase 6.0D 增量：
      1. 同 machine_id + hw_fingerprint 比对：不一致计数为克隆嫌疑（默认 loose）；
         strict 模式抛 CloneSuspectBlocked 由路由转 HTTP 409。
      2. 未命中 machine_id 但带 legacy_machine_id → 走迁移分支（_try_migrate_legacy）。
      3. 全新员工：写入 hw_fingerprint / legacy_machine_id（若 client 提供）。
    """
    now = datetime.now(timezone.utc)

    employee = get_employee_by_machine(db, report.machine_id)
    if employee is not None:
        # 分支 A：machine_id 已知 → 续保 / 克隆嫌疑检测
        _apply_hw_fingerprint(employee, report, now)
        if (
            settings.device_identity_strict_mode
            and employee.clone_suspect_last_at == now
        ):
            # _apply_hw_fingerprint 刚刚把 last_at 设为 now ⇒ 本次刚命中嫌疑
            # 在 strict 模式下回滚本会话并抛异常，避免污染 employee/activity_log
            db.rollback()
            raise CloneSuspectBlocked(
                employee_id=employee.id,
                machine_id=report.machine_id,
                expected_fp=employee.hw_fingerprint or "",
                got_fp=report.hw_fingerprint or "",
            )
    else:
        # 分支 B：machine_id 不在表中 → 尝试 legacy 迁移
        employee = _try_migrate_legacy(db, report, now)
        if employee is None:
            # 分支 C：新员工自动注册（含 6.0D 新列；老 client 时这些为 None/0）
            employee = models.Employee(
                machine_id=report.machine_id,
                name=report.name or report.machine_id,
                hostname=report.hostname,
                hw_fingerprint=report.hw_fingerprint,
                hw_fingerprint_first_seen=now if report.hw_fingerprint else None,
                legacy_machine_id=report.legacy_machine_id,
            )
            db.add(employee)
            db.flush()  # 取得自增 id
            # 低频关键事件：新员工自动注册
            logger.info(
                "新员工自动注册：name=%s machine_id=%s hostname=%s hw_fp=%s",
                employee.name,
                employee.machine_id,
                employee.hostname,
                (employee.hw_fingerprint or "")[:8] or "<none>",
            )

    # 客户端可能在后续上报中补充/更新姓名、主机名
    # ------------------------------------------------------------------
    # Phase 6.4A · name protection
    #   规则：never overwrite an existing real name with a different one.
    #   - 空 / 与 machine_id 相同（自动注册时的占位符）→ 用上报的 name 升级
    #   - 已被设成"真名"（admin 在管理后台改过 / 6.4A 安装向导写过）→
    #     保留，**即使** client 上报了不同的 name 也不动
    #   这样 admin 在管理后台改名后，agent 后续上报不会把它复原。
    # ------------------------------------------------------------------
    if report.name:
        existing = (employee.name or "").strip()
        is_placeholder = (not existing) or (existing == employee.machine_id)
        if is_placeholder:
            employee.name = report.name
        elif existing != report.name:
            logger.debug(
                "name-preserve: keeping existing=%r despite report=%r (mid=%s)",
                existing, report.name, employee.machine_id,
            )
    if report.hostname:
        employee.hostname = report.hostname

    status = _resolve_reported_status(report.idle_seconds)
    employee.status = status.value
    employee.idle_seconds = report.idle_seconds
    employee.last_seen = now

    log = models.ActivityLog(
        employee_id=employee.id,
        status=status.value,
        is_active=report.is_active,
        idle_seconds=report.idle_seconds,
        reported_at=now,
    )
    db.add(log)
    db.commit()
    db.refresh(employee)
    return employee, log


def effective_status(
    employee: models.Employee, now: datetime | None = None
) -> EmployeeStatus:
    """实时状态：长时间未上报视为离线，否则采用最近一次上报的状态。"""
    now = now or datetime.now(timezone.utc)
    if employee.last_seen is None:
        return EmployeeStatus.offline

    last_seen = employee.last_seen
    if last_seen.tzinfo is None:  # SQLite 取回的 datetime 可能不带时区
        last_seen = last_seen.replace(tzinfo=timezone.utc)

    if (now - last_seen).total_seconds() > settings.offline_threshold_seconds:
        return EmployeeStatus.offline
    return EmployeeStatus(employee.status)

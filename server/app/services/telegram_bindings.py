"""Telegram 绑定服务：employee ↔ telegram_user_id 的 1:1 管理。

身份核心是 employee / machine_id / 客户端；Telegram 仅作操作入口。绑定可由后台
自由 bind / rebind / unbind。鉴权一律实时按 telegram_user_id 查库、不缓存，故改绑后
新号立即生效、旧号立即失效。1:1 双向约束由 telegram_users 唯一索引 + 入库前预检共同保证。
"""
import logging

from sqlalchemy import select
from sqlalchemy.orm import Session

from ..models import Employee, TelegramSeenUser, TelegramUser, utcnow

logger = logging.getLogger(__name__)


class BindingError(Exception):
    """绑定相关业务错误基类（携带建议的 HTTP 状态码）。"""

    status_code = 400


class EmployeeNotFound(BindingError):
    status_code = 404


class BindingNotFound(BindingError):
    status_code = 404


class BindingConflict(BindingError):
    status_code = 409


def get_employee_id_by_tg(db: Session, tg_user_id: int) -> int | None:
    """handler 鉴权用：按 telegram_user_id 实时查绑定的员工 id（未绑定返回 None）。"""
    row = db.scalar(
        select(TelegramUser).where(TelegramUser.telegram_user_id == tg_user_id)
    )
    return row.employee_id if row else None


def get_binding_by_employee(db: Session, employee_id: int) -> TelegramUser | None:
    return db.scalar(
        select(TelegramUser).where(TelegramUser.employee_id == employee_id)
    )


def get_binding_by_tg(db: Session, tg_user_id: int) -> TelegramUser | None:
    return db.scalar(
        select(TelegramUser).where(TelegramUser.telegram_user_id == tg_user_id)
    )


def list_bindings(db: Session) -> list[TelegramUser]:
    return list(db.scalars(select(TelegramUser).order_by(TelegramUser.employee_id)))


def bind(
    db: Session, employee_id: int, tg_user_id: int, username: str | None = None
) -> TelegramUser:
    """新增绑定。员工已绑、tg 已占用或员工已离职 → BindingConflict(409)。"""
    employee = db.get(Employee, employee_id)
    if employee is None:
        raise EmployeeNotFound(f"员工不存在: {employee_id}")
    if not employee.is_active:
        raise BindingConflict(f"员工已离职，请先恢复员工 {employee_id}")
    if get_binding_by_employee(db, employee_id) is not None:
        raise BindingConflict(f"员工 {employee_id} 已绑定 Telegram，请改用改绑")
    other = get_binding_by_tg(db, tg_user_id)
    if other is not None:
        raise BindingConflict(f"该 Telegram 用户已绑定到员工 {other.employee_id}")

    row = TelegramUser(
        employee_id=employee_id,
        telegram_user_id=tg_user_id,
        telegram_username=username,
    )
    db.add(row)
    db.commit()
    db.refresh(row)
    logger.info("绑定 employee_id=%s tg_user_id=%s", employee_id, tg_user_id)
    return row


def rebind(
    db: Session, employee_id: int, new_tg_user_id: int, username: str | None = None
) -> TelegramUser:
    """改绑（换 Telegram 账号）。新号被他人占用或员工已离职 → 409；员工无绑定 → 404。

    改绑后旧号因鉴权实时查库而立即失效，新号立即生效。
    """
    row = get_binding_by_employee(db, employee_id)
    if row is None:
        raise BindingNotFound(f"员工 {employee_id} 尚无绑定，请先绑定")
    employee = db.get(Employee, employee_id)
    if employee is None or not employee.is_active:
        raise BindingConflict(f"员工已离职，请先恢复员工 {employee_id}")
    other = get_binding_by_tg(db, new_tg_user_id)
    if other is not None and other.employee_id != employee_id:
        raise BindingConflict(f"该 Telegram 用户已绑定到员工 {other.employee_id}")

    old = row.telegram_user_id
    row.telegram_user_id = new_tg_user_id
    if username is not None:
        row.telegram_username = username
    db.commit()
    db.refresh(row)
    logger.info("改绑 employee_id=%s tg %s -> %s", employee_id, old, new_tg_user_id)
    return row


def unbind(db: Session, employee_id: int) -> bool:
    """解绑。返回是否确有绑定被删除（无绑定时幂等返回 False）。"""
    row = get_binding_by_employee(db, employee_id)
    if row is None:
        return False
    db.delete(row)
    db.commit()
    logger.info("解绑 employee_id=%s", employee_id)
    return True


def upsert_seen_user(
    db: Session, tg_user_id: int, username: str | None = None
) -> TelegramSeenUser:
    """记录/更新与 bot 交互过的 Telegram 用户（Round B 的 poller 调用）。"""
    row = db.get(TelegramSeenUser, tg_user_id)
    now = utcnow()
    if row is None:
        row = TelegramSeenUser(
            telegram_user_id=tg_user_id,
            telegram_username=username,
            first_seen=now,
            last_seen=now,
        )
        db.add(row)
    else:
        row.last_seen = now
        if username is not None:
            row.telegram_username = username
    db.commit()
    return row


def list_unbound_recent(db: Session, limit: int = 100) -> list[TelegramSeenUser]:
    """最近与 bot 交互但尚未绑定的 Telegram 用户，last_seen 倒序。"""
    bound = select(TelegramUser.telegram_user_id)
    stmt = (
        select(TelegramSeenUser)
        .where(TelegramSeenUser.telegram_user_id.not_in(bound))
        .order_by(TelegramSeenUser.last_seen.desc())
        .limit(limit)
    )
    return list(db.scalars(stmt))

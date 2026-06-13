"""后台 Telegram 绑定管理接口。仅后台绑定，不开放 Telegram 自助绑定。"""
import logging

from fastapi import APIRouter, Depends, HTTPException, Query
from sqlalchemy.orm import Session

from .. import crud
from ..config import settings
from ..database import get_db
from ..models import TelegramUser
from ..schemas import (
    BindRequest,
    RebindRequest,
    TelegramUserOut,
    UnboundTelegramUserOut,
)
from ..services import telegram_bindings as tb

logger = logging.getLogger(__name__)

router = APIRouter(prefix="/telegram", tags=["telegram-admin"])


def _to_out(row: TelegramUser, name_map: dict[int, str]) -> TelegramUserOut:
    return TelegramUserOut(
        id=row.id,
        employee_id=row.employee_id,
        employee_name=name_map.get(row.employee_id),
        telegram_user_id=row.telegram_user_id,
        telegram_username=row.telegram_username,
        created_at=row.created_at,
        updated_at=row.updated_at,
    )


def _name_map(db: Session) -> dict[int, str]:
    return {emp.id: emp.name for emp in crud.list_employees(db)}


@router.get("/bindings", response_model=list[TelegramUserOut], summary="所有 Telegram 绑定")
def list_bindings(db: Session = Depends(get_db)) -> list[TelegramUserOut]:
    name_map = _name_map(db)
    return [_to_out(r, name_map) for r in tb.list_bindings(db)]


@router.post("/bindings", response_model=TelegramUserOut, summary="新增绑定")
def create_binding(payload: BindRequest, db: Session = Depends(get_db)) -> TelegramUserOut:
    try:
        row = tb.bind(
            db, payload.employee_id, payload.telegram_user_id, payload.telegram_username
        )
    except tb.BindingError as err:
        raise HTTPException(status_code=err.status_code, detail=str(err))
    return _to_out(row, _name_map(db))


@router.put(
    "/bindings/{employee_id}",
    response_model=TelegramUserOut,
    summary="改绑（员工更换 Telegram 账号）",
)
def update_binding(
    employee_id: int, payload: RebindRequest, db: Session = Depends(get_db)
) -> TelegramUserOut:
    try:
        row = tb.rebind(
            db, employee_id, payload.telegram_user_id, payload.telegram_username
        )
    except tb.BindingError as err:
        raise HTTPException(status_code=err.status_code, detail=str(err))
    return _to_out(row, _name_map(db))


@router.delete("/bindings/{employee_id}", summary="解绑")
def delete_binding(employee_id: int, db: Session = Depends(get_db)) -> dict:
    unbound = tb.unbind(db, employee_id)
    return {"employee_id": employee_id, "unbound": unbound}


@router.get(
    "/unbound-users",
    response_model=list[UnboundTelegramUserOut],
    summary="最近未绑定 Telegram 用户",
)
def list_unbound_users(
    limit: int = Query(100, ge=1, le=settings.max_query_limit),
    db: Session = Depends(get_db),
) -> list[UnboundTelegramUserOut]:
    return tb.list_unbound_recent(db, limit=limit)

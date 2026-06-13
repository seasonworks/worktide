"""工时/Break 查询接口（后台首页与统计图表数据源）。只读，不接 Telegram。"""
import logging
from datetime import date as date_cls

from fastapi import APIRouter, Depends, Query
from sqlalchemy.orm import Session

from ..config import settings
from ..constants import BreakType
from ..database import get_db
from ..schemas import (
    BreakSessionOut,
    DailyWorkStatsOut,
    ShiftSessionOut,
    ShiftStatsOut,
    WorkStatusOut,
)
from ..services import work_state as ws

logger = logging.getLogger(__name__)

router = APIRouter(prefix="/work", tags=["work"])


@router.get("/status", response_model=list[WorkStatusOut], summary="全员实时工时状态")
def work_status(
    include_inactive: bool = Query(False, description="是否包含已离职员工"),
    db: Session = Depends(get_db),
) -> list[dict]:
    """每个员工的活动状态（online/idle/offline）与 work_state 并存，含实时 break/shift 秒数。"""
    return ws.list_work_status(db, include_inactive=include_inactive)


@router.get(
    "/employees/{employee_id}/breaks",
    response_model=list[BreakSessionOut],
    summary="某员工 break 历史",
)
def employee_breaks(
    employee_id: int,
    limit: int = Query(100, ge=1, le=settings.max_query_limit),
    break_type: BreakType | None = Query(None, description="可选：meal/toilet/smoke"),
    date: date_cls | None = Query(None, description="可选：YYYY-MM-DD（UTC 单日）"),
    db: Session = Depends(get_db),
) -> list[BreakSessionOut]:
    return ws.list_employee_breaks(
        db, employee_id, limit=limit, break_type=break_type, day=date
    )


@router.get(
    "/employees/{employee_id}/shifts",
    response_model=list[ShiftSessionOut],
    summary="某员工 shift 历史",
)
def employee_shifts(
    employee_id: int,
    limit: int = Query(100, ge=1, le=settings.max_query_limit),
    db: Session = Depends(get_db),
) -> list[ShiftSessionOut]:
    return ws.list_employee_shifts(db, employee_id, limit=limit)


@router.get(
    "/employees/{employee_id}/shift-stats",
    response_model=ShiftStatsOut | None,
    summary="员工当前/最近一次班次统计",
)
def employee_shift_stats(
    employee_id: int,
    db: Session = Depends(get_db),
) -> dict | None:
    """进行中则统计当前班次；否则统计最近一次已结束班次；无任何班次返回 null。"""
    return ws.shift_stats(db, employee_id)


@router.get(
    "/stats/daily",
    response_model=list[DailyWorkStatsOut],
    summary="单日工时统计（按员工聚合）",
)
def daily_stats(
    date: date_cls = Query(..., description="YYYY-MM-DD（UTC 单日）"),
    include_inactive: bool = Query(False, description="是否包含已离职员工"),
    db: Session = Depends(get_db),
) -> list[dict]:
    return ws.daily_stats(db, date, include_inactive=include_inactive)

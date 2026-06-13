"""工时/Break 状态机的开发期调试接口。

直接驱动 work_state 状态机，供浏览器/Postman 手工验证“上班/休息/回座/下班/超时/
强制恢复”是否真实写入数据库。生产环境可在 main 中按需停用本 router。
不接 Telegram、不起后台线程。
"""
import logging

from fastapi import APIRouter, Depends
from sqlalchemy.orm import Session

from ..constants import BreakType
from ..database import get_db
from ..schemas import ActionResultOut, ExpireBreaksOut
from ..services import work_state as ws

logger = logging.getLogger(__name__)

router = APIRouter(prefix="/debug/work", tags=["debug-work"])


@router.post("/clock-in/{employee_id}", response_model=ActionResultOut, summary="[调试] 上班打卡")
def debug_clock_in(employee_id: int, db: Session = Depends(get_db)) -> ActionResultOut:
    return ws.clock_in(db, employee_id)


@router.post("/clock-out/{employee_id}", response_model=ActionResultOut, summary="[调试] 下班打卡")
def debug_clock_out(employee_id: int, db: Session = Depends(get_db)) -> ActionResultOut:
    return ws.clock_out(db, employee_id)


@router.post(
    "/break/{employee_id}/{break_type}",
    response_model=ActionResultOut,
    summary="[调试] 开始休息（meal/toilet/smoke）",
)
def debug_start_break(
    employee_id: int, break_type: BreakType, db: Session = Depends(get_db)
) -> ActionResultOut:
    return ws.start_break(db, employee_id, break_type)


@router.post("/back/{employee_id}", response_model=ActionResultOut, summary="[调试] 回座")
def debug_back(employee_id: int, db: Session = Depends(get_db)) -> ActionResultOut:
    return ws.return_to_work(db, employee_id)


@router.post(
    "/force-resume/{employee_id}",
    response_model=ActionResultOut,
    summary="[调试] 强制恢复在岗",
)
def debug_force_resume(employee_id: int, db: Session = Depends(get_db)) -> ActionResultOut:
    return ws.force_resume_working(db, employee_id)


@router.post("/expire-breaks", response_model=ExpireBreaksOut, summary="[调试] 手动触发超时清扫")
def debug_expire_breaks(db: Session = Depends(get_db)) -> ExpireBreaksOut:
    results = ws.expire_overdue_breaks(db)
    return ExpireBreaksOut(expired=len(results), details=results)

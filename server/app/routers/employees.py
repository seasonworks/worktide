from datetime import datetime, timezone

from fastapi import APIRouter, Depends, HTTPException, Query
from sqlalchemy.orm import Session

from .. import crud, models
from ..constants import EmployeeStatus
from ..database import get_db
from ..schemas import ActivityLogOut, EmployeeOut, EmployeeUpdate
from ..services import work_state

router = APIRouter(prefix="/employees", tags=["employees"])


def _serialize(employee: models.Employee, now: datetime) -> EmployeeOut:
    """用实时计算的状态（含 offline）覆盖存储状态后再输出。"""
    data = EmployeeOut.model_validate(employee)
    data.status = crud.effective_status(employee, now)
    return data


@router.get("", response_model=list[EmployeeOut], summary="员工列表（含实时状态）")
def list_employees(
    include_inactive: bool = Query(False, description="是否包含已离职员工"),
    db: Session = Depends(get_db),
) -> list[EmployeeOut]:
    now = datetime.now(timezone.utc)
    return [
        _serialize(e, now)
        for e in crud.list_employees(db, include_inactive=include_inactive)
    ]


@router.get("/idle", response_model=list[EmployeeOut], summary="当前挂机员工")
def list_idle_employees(db: Session = Depends(get_db)) -> list[EmployeeOut]:
    """当前挂机员工。复用 list_employees 默认仅在职。"""
    now = datetime.now(timezone.utc)
    return [
        data
        for e in crud.list_employees(db)  # 默认 include_inactive=False
        if (data := _serialize(e, now)).status == EmployeeStatus.idle
    ]


@router.get("/{employee_id}", response_model=EmployeeOut, summary="员工详情")
def get_employee(employee_id: int, db: Session = Depends(get_db)) -> EmployeeOut:
    employee = crud.get_employee(db, employee_id)
    if employee is None:
        raise HTTPException(status_code=404, detail="员工不存在")
    return _serialize(employee, datetime.now(timezone.utc))


@router.patch(
    "/{employee_id}", response_model=EmployeeOut, summary="修改员工信息（仅姓名）"
)
def patch_employee(
    employee_id: int,
    payload: EmployeeUpdate,
    db: Session = Depends(get_db),
) -> EmployeeOut:
    """后台改名。不动客户端上报流程；默认客户端 name 留空，不会覆盖此处写入。"""
    name = payload.name.strip()
    if not name:
        raise HTTPException(status_code=400, detail="姓名不能为空")
    employee = crud.update_employee(db, employee_id, name)
    if employee is None:
        raise HTTPException(status_code=404, detail="员工不存在")
    return _serialize(employee, datetime.now(timezone.utc))


@router.patch(
    "/{employee_id}/archive",
    response_model=EmployeeOut,
    summary="离职（软删除）",
)
def archive_employee(
    employee_id: int, db: Session = Depends(get_db)
) -> EmployeeOut:
    """业务失效，不删除数据：自动收口 runtime state（clock_out 幂等），写 is_active=false。

    - 若员工仍在 working/break：调 work_state.clock_out（off_shift→noop）以关闭 shift/break。
    - Telegram 绑定行保留，但 handler / bind / rebind 因 is_active=false 自动拒绝。
    - 所有历史 activity_logs / shift_sessions / break_sessions 不动。
    """
    employee = crud.get_employee(db, employee_id)
    if employee is None:
        raise HTTPException(status_code=404, detail="员工不存在")
    if not employee.is_active:
        # 幂等：已离职再 archive，原样返回
        return _serialize(employee, datetime.now(timezone.utc))
    work_state.clock_out(db, employee_id)  # off_shift→noop；working/break→正常收口
    employee = crud.archive_employee(db, employee_id)
    return _serialize(employee, datetime.now(timezone.utc))


@router.patch(
    "/{employee_id}/restore",
    response_model=EmployeeOut,
    summary="恢复员工（取消离职）",
)
def restore_employee(
    employee_id: int, db: Session = Depends(get_db)
) -> EmployeeOut:
    """is_active=true、deleted_at=null；不自动 clock_in，从 off_shift 重启。"""
    employee = crud.get_employee(db, employee_id)
    if employee is None:
        raise HTTPException(status_code=404, detail="员工不存在")
    employee = crud.restore_employee(db, employee_id)
    return _serialize(employee, datetime.now(timezone.utc))


@router.get(
    "/{employee_id}/logs",
    response_model=list[ActivityLogOut],
    summary="员工历史上报记录",
)
def get_employee_logs(
    employee_id: int,
    limit: int = Query(100, ge=1, le=1000),
    db: Session = Depends(get_db),
) -> list[models.ActivityLog]:
    if crud.get_employee(db, employee_id) is None:
        raise HTTPException(status_code=404, detail="员工不存在")
    return crud.list_logs(db, employee_id=employee_id, limit=limit)

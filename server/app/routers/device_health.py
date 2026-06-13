"""Phase 5.3 · Device Health API。

3 个 endpoint：
- ``POST /api/v1/agent/health``         客户端 HealthReporter 上报（120s 一次）
- ``GET  /api/v1/devices``              Admin UI 列表
- ``GET  /api/v1/devices/{employee_id}`` Admin UI 详情

设计要点（与 design doc 决策一致）：
- **D2 no auth**：与项目其它 endpoint 口径一致
- **D6 server-side status**：5 档状态仅在 service.compute_status 计算，
  client 只报原始信号
- 列表 / 详情口径分离：watchdog 子结构只出现在详情（设计稿简化）
"""
import logging

from fastapi import APIRouter, Depends, HTTPException, Query
from sqlalchemy.orm import Session

from ..database import get_db
from ..schemas import (
    DeviceDetailOut,
    DeviceListItemOut,
    HealthReportIn,
    HealthReportOut,
)
from ..services import device_health as device_health_service

logger = logging.getLogger(__name__)

# 注意：路由分两段 prefix 设计 — POST 在 /agent/health，
# GET 在 /devices/*。两条线语义不同，硬合并到 /devices 反而违和。
router = APIRouter(tags=["device-health"])


@router.post(
    "/agent/health",
    response_model=HealthReportOut,
    summary="客户端设备健康心跳上报（120s 一次）",
)
def report_agent_health(
    payload: HealthReportIn, db: Session = Depends(get_db)
) -> HealthReportOut:
    """接收客户端 HealthReporter 的心跳。

    - machine_id 已注册 → upsert + 关联到员工
    - machine_id 未注册 → 仍然落库，employee_id=NULL（先存 health，
      等首次 /activity/report 注册员工后通过反向 join 自动可见）
    - 失败语义：服务端异常导致上报失败 → 客户端 HealthReporter 会
      日志 + 退避重试；agent 主循环不受影响
    """
    try:
        device_health_service.upsert_health(db, payload)
    except Exception:  # noqa: BLE001
        logger.exception("upsert_health 失败 machine_id=%s", payload.machine_id)
        # 抛 500，让 client 退避重试
        raise HTTPException(status_code=500, detail="upsert_health 失败")
    return HealthReportOut(status="received")


@router.get(
    "/devices",
    response_model=list[DeviceListItemOut],
    summary="设备健康列表（Admin UI）",
)
def list_devices(
    db: Session = Depends(get_db),
    include_inactive: bool = Query(
        False, description="是否包含已归档（is_active=False）员工的设备"
    ),
) -> list[DeviceListItemOut]:
    """列出所有上报过 health 的设备，按 last_report_at desc 排序。
    状态由 service.compute_status 实时算（D6），不需要客户端预判。"""
    return device_health_service.list_devices(db, include_inactive=include_inactive)


@router.get(
    "/devices/{employee_id}",
    response_model=DeviceDetailOut,
    summary="设备健康详情（Admin UI）",
)
def get_device_detail(
    employee_id: int, db: Session = Depends(get_db)
) -> DeviceDetailOut:
    """按 employee_id 查详情。该员工从未上报 health → 404。"""
    detail = device_health_service.get_device_detail(db, employee_id)
    if detail is None:
        raise HTTPException(
            status_code=404,
            detail=f"employee {employee_id} 还没有 device health 记录",
        )
    return detail

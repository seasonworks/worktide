import logging
import threading
from datetime import datetime, timedelta

from fastapi import APIRouter, BackgroundTasks, Depends, HTTPException, Query
from sqlalchemy import select
from sqlalchemy.orm import Session

from .. import crud
from ..config import settings
from ..constants import EmployeeStatus
from ..database import get_db
from ..models import ActivityLog
from ..notifier import notifier
from ..schemas import ActivityReport, RecentActivityOut, ReportResponse
from ..services import auto_return, manual_punch, work_state
from ..services.settings_service import settings_service

logger = logging.getLogger(__name__)

router = APIRouter(prefix="/activity", tags=["activity"])


# ---------------------------------------------------------------------------
# Phase 6.5A · GS-4 / GS-5 进程内活动状态转换检测
#
# 设计取舍：
#   - 进程内 dict，重启丢失（与 notifier._cooldown 同模式；丢失最多多发一次）
#   - 永远更新 _prev_status（即便 paused / 离职），避免 break 结束那一刻误触发 idle_exit
#   - 转换分类：
#       prev != "idle" AND curr == "idle"   → enter（首次进入空闲）
#       prev == "idle" AND curr == "online" → exit（仅"恢复活跃"算 exit，
#                                                   idle→offline 不算）
#   - notifier 内部已 gate enabled 与 cooldown，本层不重复判定
# ---------------------------------------------------------------------------

_status_lock = threading.Lock()
_prev_status: dict[int, str] = {}
_idle_started_at: dict[int, datetime] = {}
# 已发过挂机提醒的"第几个 idle_threshold 段"：idle_seconds // threshold。
# 每跨过一个阈值段（15/30/45 min…）最多发一次；退出挂机即清空，下次重新计。
_idle_notified_bucket: dict[int, int] = {}
# 本进程已"首见回填"过的员工。进程内 dict 重启即丢，导致重启后首条上报会把
# 重启前就已挂机的员工误当成新 idle_enter 重复 @、且 bucket 归零重发当前段。
# 首见时从 DB 上一条日志回填上述状态，使重启对通知透明。
_seeded_after_start: set[int] = set()


def _seed_first_sight(db: Session, log: ActivityLog, threshold: int) -> None:
    """进程（重）启动后首次见到某员工时，从上一条 ActivityLog 回填转换状态。

    - 回填 _prev_status：避免"重启前已挂机、重启后仍挂机"被当成新 idle_enter 重复 @；
      同时保留"重启前 online、现在 idle"这类真·进入挂机的正常触发。
    - 若上一条与当前都为 idle：回填 _idle_notified_bucket（当前段，抑制重发当前阈值段）
      与 _idle_started_at（now - idle_seconds，挂机真实起点）。
    只在每员工每进程跑一次（_seeded_after_start 守卫）。
    """
    emp_id = log.employee_id
    with _status_lock:
        if emp_id in _seeded_after_start:
            return
        _seeded_after_start.add(emp_id)
        if emp_id in _prev_status:
            return  # 进程已在跟踪该员工，无需回填
    prev_status = db.scalars(
        select(ActivityLog.status)
        .where(ActivityLog.employee_id == emp_id, ActivityLog.id < log.id)
        .order_by(ActivityLog.id.desc())
        .limit(1)
    ).first()
    if prev_status is None:
        return  # 该员工史上第一条上报，本就是真·首次，无需回填
    with _status_lock:
        # 仅当仍未被并发填充时回填，避免覆盖已发生的真实转换
        _prev_status.setdefault(emp_id, prev_status)
        if (
            prev_status == EmployeeStatus.idle.value
            and log.status == EmployeeStatus.idle.value
        ):
            _idle_notified_bucket.setdefault(emp_id, log.idle_seconds // max(1, threshold))
            _idle_started_at.setdefault(
                emp_id, log.reported_at - timedelta(seconds=max(0, log.idle_seconds))
            )


def _classify_transition(
    employee_id: int, curr_status: str, now: datetime, idle_seconds: int = 0
) -> tuple[str | None, datetime | None]:
    """登记当前状态并返回 (event_type, idle_started_at)。

    event_type:
        "enter" - 进入挂机
        "exit"  - 退出挂机回到 active
        None    - 无事件
    idle_started_at:
        仅 event=="exit" 时返回起始 UTC 时间，供计算挂机时长用
    """
    with _status_lock:
        prev = _prev_status.get(employee_id)
        _prev_status[employee_id] = curr_status
        if prev != "idle" and curr_status == "idle":
            # Bug-012：挂机检测要等空闲累计到阈值（idle_threshold_seconds）才触发，
            # 故真正的挂机起点是"当下 - 已空闲秒数"，不是检测到的这一刻；否则
            # idle_exit 的挂机时长会少算一整个阈值（如阈值 15min，实际挂机 17min 却报 2min）。
            _idle_started_at[employee_id] = now - timedelta(seconds=max(0, idle_seconds))
            return "enter", None
        if prev == "idle" and curr_status == "online":
            started = _idle_started_at.pop(employee_id, None)
            return "exit", started
        return None, None


def _reset_transition_state() -> None:
    """供 smoke / 单测用。生产路径不调。"""
    with _status_lock:
        _prev_status.clear()
        _idle_started_at.clear()
        _idle_notified_bucket.clear()
        _seeded_after_start.clear()


@router.post("/report", response_model=ReportResponse, summary="客户端状态上报")
def report_activity(
    report: ActivityReport,
    background_tasks: BackgroundTasks,
    db: Session = Depends(get_db),
) -> ReportResponse:
    """客户端每 30 秒调用一次。

    - 首次上报会按 machine_id 自动注册员工。
    - 服务端根据空闲秒数判定 online / idle。
    - 判定为挂机时，通过后台任务触发 Telegram 通知（带冷却，不阻塞响应）。
    - Phase 6.0D 严格模式下：machine_id 与 hw_fingerprint 不匹配 → HTTP 409。
    """
    try:
        employee, log = crud.record_report(db, report)
    except crud.CloneSuspectBlocked as exc:
        # Phase 6.0D · strict 模式专用：路由层把领域异常转 409，避免污染上报链。
        # loose 模式不会走到这里（crud 内部仅 warn + 计数）。
        logger.warning(
            "拒绝克隆嫌疑上报 emp=%s machine_id=%s",
            exc.employee_id, exc.machine_id,
        )
        raise HTTPException(
            status_code=409,
            detail="machine_id 已与不同硬件指纹绑定；请联系管理员核对设备身份",
        ) from exc
    is_idle_alert = log.status == EmployeeStatus.idle.value

    # 高频路径，记 DEBUG，默认 INFO 级别下不输出，避免刷屏
    logger.debug(
        "收到上报 machine_id=%s status=%s idle=%ss",
        report.machine_id,
        log.status,
        log.idle_seconds,
    )

    # ------------------------------------------------------------------
    # Phase 6.5A · GS-4 / GS-5 · 状态转换通知
    # ------------------------------------------------------------------
    threshold = max(1, int(settings_service.get("idle_threshold_seconds")))
    # 进程（重）启动后首次见到该员工：从 DB 回填转换状态，避免重启把"重启前已挂机"
    # 误判成新 idle_enter 重复 @、bucket 归零重发当前段（进程内 dict 重启即丢）。
    _seed_first_sight(db, log, threshold)
    # 一律更新 _prev_status（即便 paused / 离职），避免 break 结束瞬间误触发
    # idle_exit。但是否触发 *通知* 仍受门控：离职员工 / break 中 暂停推送。
    event, idle_started = _classify_transition(
        employee.id, log.status, log.reported_at, log.idle_seconds,
    )
    paused = work_state.is_detection_paused(db, employee.id) if employee.is_active else True
    # Phase 6.5A · BUG-009：off_shift 员工不参与 idle_enter/idle_exit
    # 与 `paused`（仅 break_*）并列的硬 gate，不受 break_pauses_detection 开关影响
    off_shift = work_state.is_off_shift(db, employee.id) if employee.is_active else False

    notifiable = employee.is_active and not paused and not off_shift

    if log.status == EmployeeStatus.idle.value:
        # 挂机中：每跨过一个 idle_threshold 段（15/30/45 min…）发一次提醒。
        # bucket = 已挂机秒数 // 阈值；只在 bucket 增大时发，故同一段内不重复。
        if notifiable:
            bucket = log.idle_seconds // threshold
            with _status_lock:
                new_bucket = bucket >= 1 and bucket > _idle_notified_bucket.get(employee.id, 0)
                if new_bucket:
                    _idle_notified_bucket[employee.id] = bucket
            if new_bucket:
                # BUG-010 · 仅"首段(enter)"受自动回座边界抑制约束（避免与 Break Timeout
                # 撞同一分钟）；后续重复段（30/45 min…）与自动回座无关，正常发。
                if event == "enter" and auto_return.is_suppressed(employee.id, log.reported_at):
                    logger.info(
                        "员工 %s 近 %ss 内系统自动回座，抑制本段 idle 提醒",
                        employee.id, auto_return.SUPPRESS_SECONDS,
                    )
                else:
                    background_tasks.add_task(
                        notifier.notify_idle_enter,
                        employee_id=employee.id,
                        employee_name=employee.name,
                        idle_seconds=log.idle_seconds,
                    )
        elif event == "enter":
            # 保留既有日志风格，便于运维定位"为什么没收到通知"
            if not employee.is_active:
                logger.info("员工 %s 已离职，跳过挂机进入通知", employee.id)
            elif off_shift:
                logger.info("员工 %s 已下班 (off_shift)，跳过挂机进入通知", employee.id)
            else:
                logger.info("员工 %s 处于 break，暂停挂机进入通知", employee.id)
    else:
        # 不在挂机：清空 bucket，使下次重新挂机从第一段重新计
        with _status_lock:
            _idle_notified_bucket.pop(employee.id, None)
        if event == "exit" and notifiable and idle_started is not None:
            # Phase 6.5A · UX-003：60s 内有手动 Punch（BTW / clock_in / break_*）
            # 则跳过 idle_exit，避免 "🟢 Working" + "🟢 Active 恢复工作" 双消息困惑
            if manual_punch.is_suppressed(employee.id, log.reported_at):
                logger.info(
                    "员工 %s 近 %ss 内有手动 Punch，抑制 idle_exit 通知",
                    employee.id, manual_punch.SUPPRESS_SECONDS,
                )
            else:
                duration = max(0, int((log.reported_at - idle_started).total_seconds()))
                background_tasks.add_task(
                    notifier.notify_idle_exit,
                    employee_id=employee.id,
                    employee_name=employee.name,
                    idle_duration_seconds=duration,
                )

    return ReportResponse(
        employee_id=employee.id,
        status=EmployeeStatus(log.status),
        is_idle_alert=is_idle_alert,
        server_time=log.reported_at,
    )


@router.get(
    "/recent", response_model=list[RecentActivityOut], summary="最近活动记录"
)
def recent_activity(
    limit: int = Query(100, ge=1, le=settings.max_query_limit),
    employee_id: int | None = Query(None, description="可选：仅查看某员工"),
    db: Session = Depends(get_db),
) -> list[RecentActivityOut]:
    """跨员工的近期历史记录流（含姓名、电脑名）。limit 上限受 max_query_limit 约束。"""
    logs = crud.list_recent_logs(db, limit=limit, employee_id=employee_id)
    return [
        RecentActivityOut(
            id=log.id,
            employee_id=log.employee_id,
            employee_name=log.employee.name,
            hostname=log.employee.hostname,
            status=EmployeeStatus(log.status),
            is_active=log.is_active,
            idle_seconds=log.idle_seconds,
            reported_at=log.reported_at,
        )
        for log in logs
    ]

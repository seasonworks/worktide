"""Phase 4.1 · 窗口活动追踪 API（Analytics Layer，独立路由）。

Step 1 范围：
- POST /windows/report：raw 落库 + client_event_id 幂等去重；不触发聚合（Step 2 接入 aggregator）
- GET /windows/employees/{id}：查 window_sessions 当日历史（Step 2 才会有数据）
- GET /windows/stats/daily：当日按员工 + 按 process 聚合（Step 2 才会有数据）

与 /activity/report 严格解耦：独立 endpoint / 独立 schema / 独立 service / 独立 table。
"""
import logging
from collections import defaultdict
from datetime import date as date_cls
from datetime import datetime, timedelta, timezone

from fastapi import APIRouter, Depends, HTTPException, Query
from sqlalchemy import select
from sqlalchemy.dialects.sqlite import insert as sqlite_insert
from sqlalchemy.orm import Session

from .. import crud
from ..config import settings
from ..database import get_db
from ..models import Employee, WindowEventRaw, WindowSession
from ..schemas import (
    WindowAppStatOut,
    WindowDailyStatsOut,
    WindowReportIn,
    WindowReportOut,
    WindowSessionOut,
)
from ..services import window_aggregator

logger = logging.getLogger(__name__)

router = APIRouter(prefix="/windows", tags=["windows"])


def _day_bounds_utc(day: date_cls) -> tuple[datetime, datetime]:
    start = datetime(day.year, day.month, day.day, tzinfo=timezone.utc)
    return start, start + timedelta(days=1)


@router.post(
    "/report",
    response_model=WindowReportOut,
    summary="客户端窗口会话事件批量上报（raw）",
)
def report_windows(
    payload: WindowReportIn, db: Session = Depends(get_db)
) -> WindowReportOut:
    """接收客户端本地合并后的会话事件，落 window_events_raw。

    - machine_id 未注册 → 400（与 /activity/report 不同，不自动注册；窗口活动属附加 layer）
    - 离职员工：仍 200 入库（与 /activity/report 行为一致；统计时由默认过滤剔除）
    - client_event_id 已存在 → 计入 deduped，跳过
    - 单条 ended_at < started_at → 422 整批拒绝
    - 单批超 `window_report_max_batch` 由 pydantic max_length=500 自动 422
    - 聚合到 window_sessions 在 Step 2 由 aggregator 处理；本接口 Step 1 仅 raw 落库
    """
    employee = crud.get_employee_by_machine(db, payload.machine_id)
    if employee is None:
        raise HTTPException(status_code=400, detail="machine_id 未注册")

    if not payload.events:
        return WindowReportOut(accepted=0, deduped=0)

    # 批内基本校验：时间序合理
    for e in payload.events:
        if e.ended_at < e.started_at:
            raise HTTPException(
                status_code=422,
                detail=f"event {e.client_event_id} ended_at < started_at",
            )

    # client_event_id 是幂等键。三层防重，确保重传 / 并发重叠 / 批内重复都不致 500（Bug-011）：
    #   1) 批内去重：同一 payload 里重复的 client_event_id 只保留首条
    #   2) 预查已提交行：算准 accepted/deduped，并决定是否触发聚合
    #   3) 落库用 ON CONFLICT DO NOTHING：兜住"并发请求抢先提交同 id"的竞态，
    #      让重复行被静默跳过，而非抛 IntegrityError 炸掉整批事务
    seen_in_batch: set[str] = set()
    unique_events = []
    for e in payload.events:
        if e.client_event_id in seen_in_batch:
            continue
        seen_in_batch.add(e.client_event_id)
        unique_events.append(e)

    existing_ids = set(
        db.scalars(
            select(WindowEventRaw.client_event_id).where(
                WindowEventRaw.client_event_id.in_(seen_in_batch)
            )
        ).all()
    )

    rows_to_insert = [
        {
            "employee_id": employee.id,
            "client_event_id": e.client_event_id,
            "process_name": e.process_name,
            "window_title": e.window_title,
            "started_at": e.started_at,
            "ended_at": e.ended_at,
            "duration_seconds": e.duration_seconds,
            "had_input": e.had_input,
        }
        for e in unique_events
        if e.client_event_id not in existing_ids
    ]

    accepted = 0
    if rows_to_insert:
        stmt = (
            sqlite_insert(WindowEventRaw)
            .values(rows_to_insert)
            .on_conflict_do_nothing(index_elements=["client_event_id"])
        )
        result = db.execute(stmt)
        # SQLite OR IGNORE 的 rowcount = 实际插入行数；竞态下被跳过的不计入
        accepted = (
            result.rowcount
            if result.rowcount is not None and result.rowcount >= 0
            else len(rows_to_insert)
        )
        # 仅对该员工做聚合（pick up 也包括早先未 processed 的 raw，幂等安全）
        if accepted:
            db.flush()  # 让 aggregator 能看到本批新插入的 raw
            agg = window_aggregator.aggregate_raw_events(db, employee_id=employee.id)
            logger.debug(
                "windows.report employee_id=%s accepted=%s agg=%s",
                employee.id,
                accepted,
                agg,
            )

    deduped = len(payload.events) - accepted
    db.commit()  # 单事务一并 commit raw + sessions
    return WindowReportOut(accepted=accepted, deduped=deduped)


@router.get(
    "/employees/{employee_id}",
    response_model=list[WindowSessionOut],
    summary="某员工某日窗口会话历史（时间倒序）",
)
def employee_windows(
    employee_id: int,
    date: date_cls = Query(..., description="YYYY-MM-DD（UTC 单日）"),
    limit: int = Query(200, ge=1, le=settings.max_query_limit),
    db: Session = Depends(get_db),
) -> list[WindowSessionOut]:
    """历史接口：无 is_active 过滤（离职员工历史可查）。返回 window_sessions（聚合后）。"""
    start, end = _day_bounds_utc(date)
    rows = db.scalars(
        select(WindowSession)
        .where(
            WindowSession.employee_id == employee_id,
            WindowSession.started_at >= start,
            WindowSession.started_at < end,
        )
        .order_by(WindowSession.started_at.desc())
        .limit(limit)
    ).all()
    return list(rows)


@router.get(
    "/stats/daily",
    response_model=list[WindowDailyStatsOut],
    summary="单日按员工+按应用聚合的窗口活动时长",
)
def daily_window_stats(
    date: date_cls = Query(..., description="YYYY-MM-DD（UTC 单日）"),
    include_inactive: bool = Query(False, description="是否包含已离职员工"),
    top_n: int = Query(20, ge=1, le=100, description="每员工返回前 N 个应用"),
    db: Session = Depends(get_db),
) -> list[WindowDailyStatsOut]:
    """按员工 + 按 process_name 聚合，按 work_state 拆分时长。

    默认仅在职员工；离职员工的窗口数据已入库，可通过 include_inactive=true 查询。
    """
    start, end = _day_bounds_utc(date)

    emp_stmt = select(Employee).order_by(Employee.name)
    if not include_inactive:
        emp_stmt = emp_stmt.where(Employee.is_active.is_(True))
    employees = db.scalars(emp_stmt).all()

    sessions = db.scalars(
        select(WindowSession).where(
            WindowSession.started_at >= start,
            WindowSession.started_at < end,
        )
    ).all()

    # employee_id -> { process_name -> {working,break,off_shift} }
    per_emp: dict[int, dict[str, dict[str, int]]] = defaultdict(
        lambda: defaultdict(lambda: {"working": 0, "break": 0, "off_shift": 0})
    )
    totals: dict[int, dict[str, int]] = defaultdict(
        lambda: {"working": 0, "break": 0, "off_shift": 0}
    )
    for s in sessions:
        bucket = (
            "working"
            if s.work_state == "working"
            else "off_shift"
            if s.work_state == "off_shift"
            else "break"
        )
        per_emp[s.employee_id][s.process_name][bucket] += s.duration_seconds
        totals[s.employee_id][bucket] += s.duration_seconds

    out: list[WindowDailyStatsOut] = []
    for emp in employees:
        apps_map = per_emp.get(emp.id, {})
        top_apps = sorted(
            (
                WindowAppStatOut(
                    process_name=name,
                    working_seconds=parts["working"],
                    break_seconds=parts["break"],
                    off_shift_seconds=parts["off_shift"],
                    total_seconds=parts["working"] + parts["break"] + parts["off_shift"],
                )
                for name, parts in apps_map.items()
            ),
            key=lambda a: a.total_seconds,
            reverse=True,
        )[:top_n]
        t = totals.get(emp.id, {"working": 0, "break": 0, "off_shift": 0})
        out.append(
            WindowDailyStatsOut(
                employee_id=emp.id,
                name=emp.name,
                date=date.isoformat(),
                total_working_seconds=t["working"],
                total_break_seconds=t["break"],
                total_off_shift_seconds=t["off_shift"],
                top_apps=top_apps,
            )
        )
    return out

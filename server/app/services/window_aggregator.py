"""Phase 4.1 · 窗口活动聚合器（Analytics Layer 核心）。

职责：把 window_events_raw 中未处理的事件按 work_state / shift 边界拆分，
然后在满足全部 merge 条件时合并到既有 window_sessions，否则新建。

设计原则：
- merge correctness > merge aggressiveness（保守，默认 merge_gap=5s）
- 跨 work_state / shift 边界**绝对不合并**（working ↔ break_* ↔ off_shift 严格隔离）
- 按 employee_id 串行处理；不 commit，由调用方包成单事务
- raw.processed_at 幂等：已处理的 raw 不再入聚合
- DEBUG 日志可见 split/merge 决策；**永不**在任何级别输出 window_title
"""
from __future__ import annotations

import logging
import re
from collections import defaultdict
from datetime import datetime, timezone

from sqlalchemy import or_, select
from sqlalchemy.orm import Session

from ..config import settings
from ..models import (
    BreakSession,
    ShiftSession,
    WindowEventRaw,
    WindowSession,
    utcnow,
)

logger = logging.getLogger(__name__)

# 保守 merge gap：避免跨段错误粘连
DEFAULT_MERGE_GAP_SECONDS = 5

_WHITESPACE_RE = re.compile(r"[\s  -​　]+", flags=re.UNICODE)
# 匹配 "(N)" 计数标记（通知未读数、tab 索引等），周边可有空白
_COUNTER_RE = re.compile(r"\s*\(\d+\)\s*")
_COLLAPSE_RE = re.compile(r"\s+")


def normalize_window_title(title: str | None) -> str:
    """把 raw window_title 归一化为稳定 token 供 session merge 比较 / 未来 category 化。

    - unicode 空白（  /  -​ / 　 / \\t / \\n …）统一为 ASCII space
    - 移除 "(N)" 计数（通知未读数 / tab 索引，例如 "(1) Inbox" / "Page (5)"）
    - 折叠连续空白 + trim
    - 截断到 settings.window_title_max_length（默认 120）

    例：
        "YouTube (1) - Chrome"        → "YouTube - Chrome"
        " a   b\\u00a0c "              → "a b c"
        "(1) Tab (2) Browser"         → "Tab Browser"
        "(beta) App"                  → "(beta) App"   （括号内非纯数字，保留）
    """
    if not title:
        return ""
    s = _WHITESPACE_RE.sub(" ", title)
    s = _COUNTER_RE.sub(" ", s)
    s = _COLLAPSE_RE.sub(" ", s).strip()
    max_len = settings.window_title_max_length
    if len(s) > max_len:
        s = s[:max_len]
    return s


def _aware(dt: datetime | None) -> datetime | None:
    """SQLite 取回的 naive datetime 统一补 UTC。"""
    if dt is None:
        return None
    if dt.tzinfo is None:
        return dt.replace(tzinfo=timezone.utc)
    return dt


def _state_at(t: datetime, shifts, breaks) -> tuple[str, int | None]:
    """返回时间点 t 对应的 (work_state, shift_id)。

    左闭右开：覆盖 [started_at, ended_at)；ongoing 取 [started_at, ∞)。
    优先级：break_* > working > off_shift。
    """
    for b in breaks:
        bs = _aware(b.started_at)
        be = _aware(b.ended_at)
        if bs <= t and (be is None or t < be):
            return f"break_{b.break_type}", b.shift_id
    for sh in shifts:
        ss = _aware(sh.started_at)
        se = _aware(sh.ended_at)
        if ss <= t and (se is None or t < se):
            return "working", sh.id
    return "off_shift", None


def _split_event_by_timeline(raw: WindowEventRaw, shifts, breaks) -> list[tuple]:
    """把一条 raw event 按 shift/break 边界切分为多段。

    返回 [(seg_started_at, seg_ended_at, work_state, shift_id), ...]，时间严格升序，
    相邻段 work_state 或 shift_id 必不同（否则就被合并为同一段）。
    """
    e_start = _aware(raw.started_at)
    e_end = _aware(raw.ended_at)
    if e_end <= e_start:
        return []

    points: set[datetime] = {e_start, e_end}
    for sh in shifts:
        for t in (_aware(sh.started_at), _aware(sh.ended_at)):
            if t is not None and e_start < t < e_end:
                points.add(t)
    for b in breaks:
        for t in (_aware(b.started_at), _aware(b.ended_at)):
            if t is not None and e_start < t < e_end:
                points.add(t)

    sorted_pts = sorted(points)
    segs: list[tuple] = []
    for i in range(len(sorted_pts) - 1):
        seg_start = sorted_pts[i]
        seg_end = sorted_pts[i + 1]
        if seg_end <= seg_start:
            continue
        ws, shift_id = _state_at(seg_start, shifts, breaks)
        segs.append((seg_start, seg_end, ws, shift_id))
    return segs


def aggregate_raw_events(
    db: Session,
    employee_id: int | None = None,
    merge_gap_seconds: int = DEFAULT_MERGE_GAP_SECONDS,
) -> dict:
    """处理 processed_at IS NULL 的 raw events → 写 / 合并 window_sessions。

    - 按 (employee_id, started_at, id) 排序，**按员工串行**处理
    - 每条 raw 按 work_state / shift 边界切分
    - 每段在满足全部 merge 条件时合并到最近一条 session：
        (process_name, normalized_title, shift_id, work_state) 相等
        且 0 <= seg.started_at - last.ended_at <= merge_gap_seconds
    - 否则 INSERT 新 session
    - raw.processed_at = now 防止重复入聚合
    - **不 commit**；由调用方（POST /windows/report）包成单事务一并 commit
    """
    raw_q = (
        select(WindowEventRaw)
        .where(WindowEventRaw.processed_at.is_(None))
        .order_by(
            WindowEventRaw.employee_id,
            WindowEventRaw.started_at,
            WindowEventRaw.id,
        )
    )
    if employee_id is not None:
        raw_q = raw_q.where(WindowEventRaw.employee_id == employee_id)
    raws = list(db.scalars(raw_q))
    if not raws:
        return {"processed": 0, "sessions_created": 0, "sessions_extended": 0}

    raws_by_emp: dict[int, list[WindowEventRaw]] = defaultdict(list)
    for r in raws:
        raws_by_emp[r.employee_id].append(r)

    total_created = 0
    total_extended = 0
    now = utcnow()
    title_max = settings.window_title_max_length

    for emp_id, emp_raws in raws_by_emp.items():
        t_start = min(_aware(r.started_at) for r in emp_raws)
        t_end = max(_aware(r.ended_at) for r in emp_raws)

        shifts = list(
            db.scalars(
                select(ShiftSession)
                .where(
                    ShiftSession.employee_id == emp_id,
                    ShiftSession.started_at < t_end,
                    or_(
                        ShiftSession.ended_at.is_(None),
                        ShiftSession.ended_at > t_start,
                    ),
                )
                .order_by(ShiftSession.started_at)
            )
        )
        breaks = list(
            db.scalars(
                select(BreakSession)
                .where(
                    BreakSession.employee_id == emp_id,
                    BreakSession.started_at < t_end,
                    or_(
                        BreakSession.ended_at.is_(None),
                        BreakSession.ended_at > t_start,
                    ),
                )
                .order_by(BreakSession.started_at)
            )
        )

        # in-memory cache: key=(process, normalized, shift_id, ws) → 最新 session 对象
        # 避免同一聚合调用内对同一 key 反复查 DB；同时承接 INSERT 之后的合并尝试
        last_by_key: dict[tuple, WindowSession] = {}
        emp_created = 0
        emp_extended = 0

        for raw in emp_raws:
            segments = _split_event_by_timeline(raw, shifts, breaks)
            normalized = normalize_window_title(raw.window_title)
            display_title = (raw.window_title or "")[:title_max]

            if len(segments) > 1:
                logger.debug(
                    "split raw=%s into %d segments (boundary cut)", raw.id, len(segments)
                )

            for seg_start, seg_end, ws, shift_id in segments:
                duration = int((seg_end - seg_start).total_seconds())
                if duration <= 0:
                    continue

                key = (raw.process_name, normalized, shift_id, ws)
                last = last_by_key.get(key)
                if last is None:
                    # 第一次见此 key：查 DB 取最近 1 条匹配 session（可能是上一批留下的）
                    q = select(WindowSession).where(
                        WindowSession.employee_id == emp_id,
                        WindowSession.process_name == raw.process_name,
                        WindowSession.normalized_title == normalized,
                        WindowSession.work_state == ws,
                    )
                    if shift_id is None:
                        q = q.where(WindowSession.shift_id.is_(None))
                    else:
                        q = q.where(WindowSession.shift_id == shift_id)
                    q = q.order_by(WindowSession.ended_at.desc()).limit(1)
                    last = db.scalar(q)

                can_merge = False
                if last is not None:
                    last_ended = _aware(last.ended_at)
                    gap = (seg_start - last_ended).total_seconds()
                    # 0 <= gap：禁止时间回退；<= merge_gap：禁止过远跨段
                    if 0 <= gap <= merge_gap_seconds:
                        can_merge = True

                if can_merge:
                    new_end = max(_aware(last.ended_at), seg_end)
                    last_start = _aware(last.started_at)
                    last.ended_at = new_end
                    last.duration_seconds = int((new_end - last_start).total_seconds())
                    last.source_event_count = (last.source_event_count or 1) + 1
                    last_by_key[key] = last
                    emp_extended += 1
                else:
                    new_session = WindowSession(
                        employee_id=emp_id,
                        shift_id=shift_id,
                        work_state=ws,
                        process_name=raw.process_name,
                        window_title=display_title,
                        normalized_title=normalized,
                        started_at=seg_start,
                        ended_at=seg_end,
                        duration_seconds=duration,
                        source_event_count=1,
                    )
                    db.add(new_session)
                    last_by_key[key] = new_session
                    emp_created += 1

            raw.processed_at = now

        logger.debug(
            "aggregate employee=%s raws=%d created=%d extended=%d",
            emp_id,
            len(emp_raws),
            emp_created,
            emp_extended,
        )
        total_created += emp_created
        total_extended += emp_extended

    return {
        "processed": len(raws),
        "sessions_created": total_created,
        "sessions_extended": total_extended,
    }

"""客户端窗口活动 Tracker（Phase 4.1 Step 4B）。

只负责三件事：
1. 周期采样前台窗口（Win32 GetForegroundWindow / QueryFullProcessImageNameW）
2. 维护内存里 `current_session`（process / normalized_title / started_at / last_active_at）
3. flush 时通过 `WindowBuffer.append_event` 落库

明确不做：
- 网络 / HTTP / 上传 / retry
- 任何统计、聚合、判定（工作 / 休息 / 班次 / 分类 / 生产力）
- 读 buffer / 删 buffer / 知道 uploader 存在

flush 触发条件（5 个）：
    1) process 切换
    2) normalized window_title 切换
    3) idle ≥ idle_threshold_seconds
    4) 距 started_at ≥ flush_interval_seconds（避免单事件无限长）
    5) tracker.stop()

隐私红线：process_name / window_title **绝不**进入 INFO/WARNING 日志；
DEBUG 也只输出 duration、计数等不可识别字段。
"""
from __future__ import annotations

import ctypes
import logging
import re
import threading
import uuid
from ctypes import wintypes
from datetime import datetime, timezone
from pathlib import Path
from typing import Callable, Optional, Tuple

from .config import WindowTrackingConfig
from .idle import get_idle_seconds
from .window_buffer import WindowBuffer

logger = logging.getLogger(__name__)

# ---------------------------------------------------------------------------
# Win32 采样
# ---------------------------------------------------------------------------

_PROCESS_QUERY_LIMITED_INFORMATION = 0x1000

# 非 Windows 平台（例如 CI 跑单元测试）时 ctypes.windll 不存在
_user32 = getattr(ctypes, "windll", None) and ctypes.windll.user32
_kernel32 = getattr(ctypes, "windll", None) and ctypes.windll.kernel32


def _get_foreground_window_info() -> Optional[Tuple[str, str]]:
    """返回前台窗口 (process_name, window_title)；无可用窗口时返回 None。

    - process_name 取 exe 文件名（如 ``chrome.exe``），不含路径
    - title 来自 GetWindowTextW，不做规范化（交给 normalize_window_title）
    - 系统进程 / OpenProcess 失败 / 句柄为 0 时返回 None（视同 idle）
    """
    if _user32 is None or _kernel32 is None:
        return None

    hwnd = _user32.GetForegroundWindow()
    if not hwnd:
        return None

    # title
    length = _user32.GetWindowTextLengthW(hwnd)
    if length and length > 0:
        buf = ctypes.create_unicode_buffer(length + 1)
        _user32.GetWindowTextW(hwnd, buf, length + 1)
        title = buf.value or ""
    else:
        title = ""

    # pid
    pid = wintypes.DWORD(0)
    _user32.GetWindowThreadProcessId(hwnd, ctypes.byref(pid))
    if not pid.value:
        return None

    handle = _kernel32.OpenProcess(
        _PROCESS_QUERY_LIMITED_INFORMATION, False, pid.value
    )
    if not handle:
        # 系统/受保护进程拒绝访问：放弃本次采样，等下一拍
        return None
    try:
        path_buf = ctypes.create_unicode_buffer(1024)
        size = wintypes.DWORD(len(path_buf))
        ok = _kernel32.QueryFullProcessImageNameW(
            handle, 0, path_buf, ctypes.byref(size)
        )
        full_path = path_buf.value if ok else ""
    finally:
        _kernel32.CloseHandle(handle)

    process_name = Path(full_path).name if full_path else ""
    if not process_name:
        return None
    return process_name, title


# ---------------------------------------------------------------------------
# 标题规范化（与服务端 services/window_aggregator.normalize_window_title 行为对齐）
# ---------------------------------------------------------------------------
#
# 服务端：
#   _WHITESPACE_RE = re.compile(r"[\s  -​　]+")
#   _COUNTER_RE    = re.compile(r"\s*\(\d+\)\s*")
#   _COLLAPSE_RE   = re.compile(r"\s+")
#
# Python 3 中 `\s` 在 str 模式下默认包含 Unicode 空白（NBSP / 全角空格 / 各 EN/EM
# space），无需额外列出；保留两步替换以匹配服务端语义（先全空白→space，再 collapse）。

_WHITESPACE_RE = re.compile(r"\s+")
_COUNTER_RE = re.compile(r"\s*\(\d+\)\s*")


def normalize_window_title(title: Optional[str], max_length: int = 200) -> str:
    """与服务端 normalize_window_title 行为对齐：

    - None / 非 str / 空 → ""
    - 折叠所有空白（含 Unicode NBSP / 全角空格）为单个 ASCII space
    - 删除 "(N)" 计数标记（通知未读数、tab 索引等；位置不限）
    - 折叠相邻空白 + trim
    - 长度截断到 max_length（默认与 config.title_max_length 一致 = 200）

    例：
        " YouTube (1) - Chrome "  → "YouTube - Chrome"
        "Inbox (3) (Outlook)"     → "Inbox Outlook"
        "(beta) App"              → "(beta) App"   （括号内非纯数字，保留）
    """
    if not isinstance(title, str) or not title:
        return ""
    s = _WHITESPACE_RE.sub(" ", title)
    s = _COUNTER_RE.sub(" ", s)
    s = _WHITESPACE_RE.sub(" ", s).strip()
    if max_length > 0 and len(s) > max_length:
        s = s[:max_length]
    return s


# ---------------------------------------------------------------------------
# 时间工具
# ---------------------------------------------------------------------------

def _now_utc() -> datetime:
    return datetime.now(timezone.utc)


def _iso_utc(dt: datetime) -> str:
    """ISO 8601 UTC，秒精度，带 'Z' 后缀（与服务端解析口径一致）。"""
    return dt.astimezone(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


# ---------------------------------------------------------------------------
# 内存 session
# ---------------------------------------------------------------------------

class _Session:
    """采样期间在内存中持有的当前窗口会话。仅 tracker 自己用，不暴露。"""

    __slots__ = ("process_name", "window_title", "started_at", "last_active_at")

    def __init__(
        self, process_name: str, window_title: str, now: datetime
    ) -> None:
        self.process_name = process_name
        self.window_title = window_title
        self.started_at = now
        self.last_active_at = now


# ---------------------------------------------------------------------------
# Tracker
# ---------------------------------------------------------------------------

SamplerFn = Callable[[], Optional[Tuple[str, str]]]
IdleFn = Callable[[], float]
ClockFn = Callable[[], datetime]


class WindowTracker:
    """周期采样 + session 管理 + 通过 buffer 持久化。

    线程模型：自带 daemon 线程；start()/stop() 幂等。tracker 不读 buffer、
    也不知道 uploader 存在；buffer 是它**唯一**的输出口。

    依赖注入（便于测试）：
        sampler        前台窗口采样器；默认 Win32；测试可注入返回 (proc,title) 的 fn
        idle_provider  返回当前 idle 秒；默认 idle.get_idle_seconds
        clock          返回 UTC datetime；默认 datetime.now(utc)
    """

    def __init__(
        self,
        config: WindowTrackingConfig,
        buffer: WindowBuffer,
        *,
        sampler: Optional[SamplerFn] = None,
        idle_provider: Optional[IdleFn] = None,
        clock: Optional[ClockFn] = None,
        heartbeat: object = None,
    ) -> None:
        self.config = config
        self.buffer = buffer
        self._sampler: SamplerFn = sampler or _get_foreground_window_info
        self._idle: IdleFn = idle_provider or get_idle_seconds
        self._clock: ClockFn = clock or _now_utc

        self._session: Optional[_Session] = None
        self._stop = threading.Event()
        self._thread: Optional[threading.Thread] = None

        # Phase 5.2：watchdog 心跳。延迟导入 + duck typing，
        # 避免循环 import（watchdog → config 已经导入了，反向引可能成环）。
        # 接收任何带 .tick(name) 的对象；默认走 NullHeartbeat。
        if heartbeat is None:
            from .watchdog import _NullHeartbeat
            heartbeat = _NullHeartbeat()
        self._heartbeat = heartbeat

    # ------------------------------------------------------------------
    # 线程生命周期
    # ------------------------------------------------------------------

    def start(self) -> None:
        if self._thread and self._thread.is_alive():
            return
        self._stop.clear()
        self._thread = threading.Thread(
            target=self._run, name="window-tracker", daemon=True
        )
        self._thread.start()
        logger.info(
            "window tracker 启动 sample=%ss idle_threshold=%ss flush_interval=%ss",
            self.config.sample_interval_seconds,
            self.config.idle_threshold_seconds,
            self.config.flush_interval_seconds,
        )

    def stop(self) -> None:
        """停止线程；退出前把内存里残留的 session flush 到 buffer。"""
        self._stop.set()
        if self._thread:
            self._thread.join(timeout=5)
        # 触发条件 #5：stop() 必须 flush 最后一段
        self._flush_session(self._clock())
        logger.info("window tracker 已停止")

    # ------------------------------------------------------------------
    # 主循环
    # ------------------------------------------------------------------

    def _run(self) -> None:
        # Phase 5.2：watchdog WORKER_TRACKER 名常量；从 watchdog 模块取以防 typo
        from .watchdog import WORKER_TRACKER  # 延迟导入避开环

        interval = max(1, int(self.config.sample_interval_seconds))
        while not self._stop.is_set():
            # 每拍顶部 heartbeat —— 即便 tick() 抛错，本拍已经证明线程还活着
            self._heartbeat.tick(WORKER_TRACKER)
            try:
                self.tick()
            except Exception:  # noqa: BLE001  单拍异常不应终止 tracker
                logger.exception("window tracker tick 异常，已忽略")
            # Event.wait 让 stop() 立即唤醒，不必等到下一拍
            self._stop.wait(interval)

    def tick(self) -> None:
        """单次采样 + session 决策。也可被测试直接调用做确定性验证。

        注意：每拍**最多产生一次** append_event，且只有在 process/title 变化或
        flush_interval 到期 / idle / stop() 等显式触发时才调用。
        连续相同窗口期间，10 分钟也只会产生 1 个 event（用户重点要求 A/重点要求 1）。
        """
        now = self._clock()

        # 触发条件 #3：idle 超阈值 → 关掉当前 session，不开新
        idle_sec = float(self._idle())
        if idle_sec >= self.config.idle_threshold_seconds:
            self._flush_session(now)
            return

        info = self._sampler()
        if info is None:
            # 锁屏 / 最小化 / 系统进程拒绝访问：视同 idle，关 session
            self._flush_session(now)
            return

        process_name, raw_title = info
        title = normalize_window_title(
            raw_title, self.config.title_max_length
        )

        session = self._session
        if session is None:
            # 开新 session（不写库，等 flush）
            self._session = _Session(process_name, title, now)
            return

        # 触发条件 #1 / #2：process 或 normalized title 变 → flush 旧 + 开新
        if session.process_name != process_name or session.window_title != title:
            self._flush_session(now)
            self._session = _Session(process_name, title, now)
            return

        # 同 process / 同 title：续命，不写库
        session.last_active_at = now

        # 触发条件 #4：flush_interval 到 → 切片（仍是同窗口，flush 后立刻开新）
        if self.config.flush_interval_seconds > 0:
            elapsed = (now - session.started_at).total_seconds()
            if elapsed >= self.config.flush_interval_seconds:
                self._flush_session(now)
                self._session = _Session(process_name, title, now)

    # ------------------------------------------------------------------
    # flush
    # ------------------------------------------------------------------

    def _flush_session(self, now: datetime) -> None:
        """把内存 session 落到 buffer。无 session 则直接返回。

        ended_at 取 session.last_active_at（最后一次确认有输入/可见的时刻），
        而不是 now，避免把 idle 期间错误算成"在线"。
        """
        session = self._session
        if session is None:
            return
        self._session = None  # 先清，再写库；万一 append 异常也不会重复 flush

        ended = session.last_active_at
        duration = int((ended - session.started_at).total_seconds())
        if duration < 1:
            duration = 1  # 至少 1 秒，避免 0 时长 event

        ok = self.buffer.append_event(
            client_event_id=uuid.uuid4().hex,
            process_name=session.process_name,
            window_title=session.window_title,
            started_at=_iso_utc(session.started_at),
            ended_at=_iso_utc(ended),
            duration_seconds=duration,
            had_input=True,
        )
        # 隐私：禁止输出 process_name / window_title
        logger.debug("flush session duration=%ds ok=%s", duration, ok)

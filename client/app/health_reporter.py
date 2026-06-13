"""Phase 5.3 · Device Health 心跳上报。

Daemon 线程，120s 一次 POST ``/api/v1/agent/health``。
关注点与 [reporter.py]（activity 30s）和 [window_uploader.py]（窗口 batch）
完全独立 —— 它只把 lifecycle + heartbeat + watchdog + window_buffer 四处
**已经存在的本地状态**汇集成一个 payload 上报。

设计要点：
- **不**调用 ``heartbeat.tick(...)``：HealthReporter 是观察通道，不属于
  watchdog 监控的核心 worker 集合。它挂了不应触发 agent 重启（自我引用环）
- 失败处理：网络/服务端错误 → WARN log + 指数退避；**绝不**抛到外层让
  agent 主循环受影响（与 window_uploader 同口径）
- 时钟选择：``process_started_at`` / ``last_*_at`` 用 wall clock UTC（与 server
  端 schema 一致）；``uptime_seconds`` 用 monotonic 从 ``__init__`` 时间起算

依赖注入（便于测试）：
- ``session`` 注入 fake ``requests.Session``
- ``clock_wall`` / ``clock_mono`` 注入假时钟
- ``window_buffer`` 可为 None（window.enabled=False 时 buffer/tracker/uploader
  均不存在，HealthReporter 上报 ``pending_events=0``）
"""
from __future__ import annotations

import logging
import threading
import time
from datetime import datetime, timezone
from typing import Callable, Optional

import requests

from .config import HealthConfig
from .lifecycle import LifecycleRecorder
from .version import AGENT_VERSION

logger = logging.getLogger(__name__)


class HealthReporter:
    """周期 POST device health 心跳。线程模型同 WindowUploader。"""

    def __init__(
        self,
        config: HealthConfig,
        lifecycle: LifecycleRecorder,
        *,
        machine_id: str,
        hostname: str,
        employee_name: str,
        url: str,
        request_timeout_seconds: int = 10,
        session: Optional[requests.Session] = None,
        # 可选注入：watchdog 实例（snapshot_for_reporting）+ window_buffer
        # （pending stats）+ update_state（Phase 5.4 升级状态）。
        # 任一为 None 时 payload 对应字段降级
        watchdog: object = None,
        window_buffer: object = None,
        update_state: object = None,
        # 测试用：注入假时钟
        clock_wall: Callable[[], datetime] = lambda: datetime.now(timezone.utc),
        clock_mono: Callable[[], float] = time.monotonic,
        # Phase 6.0D · Device Identity Hardening；老测试不传 → 默认空
        hw_fingerprint: str = "",
        legacy_machine_id: Optional[str] = None,
    ) -> None:
        self.config = config
        self.lifecycle = lifecycle
        self.machine_id = machine_id
        self.hostname = hostname
        self.employee_name = employee_name
        self.url = url
        self.timeout = max(1, int(request_timeout_seconds))
        self.session = session or requests.Session()

        self._watchdog = watchdog
        self._window_buffer = window_buffer
        self._update_state = update_state

        self.hw_fingerprint = hw_fingerprint
        self.legacy_machine_id = legacy_machine_id
        self._clock_wall = clock_wall
        self._clock_mono = clock_mono

        # process start 用本进程视角下"HealthReporter 创建瞬间"作为锚定时间。
        # Agent.__init__ 在 main() 早期就实例化它，与真正 spawn 时刻误差 < 10 ms，
        # server 端不需要 sub-second 精度。
        self._process_started_at = self._clock_wall()
        self._start_mono = self._clock_mono()

        self._stop = threading.Event()
        self._thread: Optional[threading.Thread] = None

    # ------------------------------------------------------------------
    # 线程生命周期
    # ------------------------------------------------------------------

    def start(self) -> None:
        if not self.config.enabled:
            logger.info("health reporter 未启用（config.health.enabled=False）")
            return
        if self._thread and self._thread.is_alive():
            return
        self._stop.clear()
        self._thread = threading.Thread(
            target=self._run, name="health-reporter", daemon=True
        )
        self._thread.start()
        logger.info(
            "health reporter 启动 url=%s interval=%ds version=%s",
            self.url,
            self.config.upload_interval_seconds,
            AGENT_VERSION,
        )

    def stop(self) -> None:
        self._stop.set()
        if self._thread:
            self._thread.join(timeout=self.timeout + 2)
        logger.info("health reporter 已停止")

    # ------------------------------------------------------------------
    # 主循环
    # ------------------------------------------------------------------

    def _run(self) -> None:
        backoff = list(self.config.upload_backoff_seconds) or [30]
        backoff_idx = 0
        # 启动后立即上报一次，让 server 端尽早看到 device online（不等 120s）
        first_round = True
        while not self._stop.is_set():
            ok = False
            try:
                ok = self._report_once()
            except Exception:  # noqa: BLE001  health 失败不应打挂线程
                logger.exception("health 上报出现未预期异常")
            if ok:
                backoff_idx = 0
                wait = (
                    1 if first_round else self.config.upload_interval_seconds
                )
            else:
                wait = backoff[min(backoff_idx, len(backoff) - 1)]
                logger.warning("health 上报失败，retry in %ds", wait)
                backoff_idx += 1
            first_round = False
            self._stop.wait(wait)

    # ------------------------------------------------------------------
    # 单次上报
    # ------------------------------------------------------------------

    def _report_once(self) -> bool:
        """构造 payload 并 POST。

        :return: ``True`` = 服务端 2xx；其它情况（含网络异常）``False``
        """
        payload = self.build_payload()
        try:
            resp = self.session.post(self.url, json=payload, timeout=self.timeout)
        except requests.RequestException as e:
            logger.warning("health POST 网络异常：%s", e)
            return False
        if resp.status_code >= 500:
            logger.warning("health POST 5xx: %d", resp.status_code)
            return False
        if resp.status_code >= 400:
            # 4xx 通常是 schema 问题；记 ERROR 让我们能看到（重试不会变好）
            logger.error(
                "health POST %d body=%s", resp.status_code, resp.text[:300]
            )
            return False
        return True

    # ------------------------------------------------------------------
    # payload 构造（与 server schemas.HealthReportIn 严格对齐）
    # ------------------------------------------------------------------

    def build_payload(self) -> dict:
        """采集本地 lifecycle/heartbeat/watchdog/buffer 状态构造上报体。

        所有外部调用都包 try/except —— 哪一个数据源挂了，仍然要上报其余
        字段（"部分降级"好过"完全失联"）。
        """
        # uptime 用 monotonic 算，避免 wall clock 跳变导致负数
        uptime_seconds = int(max(0.0, self._clock_mono() - self._start_mono))

        lifecycle_data: dict = {}
        try:
            lifecycle_data = self.lifecycle.snapshot()
        except Exception:  # noqa: BLE001
            logger.exception("读取 lifecycle 快照失败")

        pending_events = 0
        if self._window_buffer is not None:
            try:
                stats = self._window_buffer.stats()
                pending_events = int(stats.get("pending", 0))
            except Exception:  # noqa: BLE001
                logger.exception("读取 window_buffer.stats() 失败")

        watchdog_payload = None
        if self._watchdog is not None:
            try:
                watchdog_payload = self._watchdog.snapshot_for_reporting()
            except Exception:  # noqa: BLE001
                logger.exception("读取 watchdog 快照失败")

        # Phase 5.4: update 子结构（duck typing；接受任何带 snapshot() 的对象）
        update_payload = None
        if self._update_state is not None:
            try:
                update_payload = self._update_state.snapshot()
            except Exception:  # noqa: BLE001
                logger.exception("读取 update_state 快照失败")

        return {
            "machine_id": self.machine_id,
            "hostname": self.hostname,
            "employee_name": self.employee_name,
            "agent_version": AGENT_VERSION,
            "process_started_at": _iso(self._process_started_at),
            "uptime_seconds": uptime_seconds,
            "first_start_at": lifecycle_data.get("first_start_at"),
            "last_start_at": lifecycle_data.get("last_start_at"),
            "last_exit_at": lifecycle_data.get("last_exit_at"),
            "last_exit_reason": lifecycle_data.get("last_exit_reason"),
            "restart_count": int(lifecycle_data.get("restart_count", 0)),
            "pending_events": pending_events,
            "watchdog": watchdog_payload,
            "update": update_payload,
            # Phase 6.0D · server 6.0D-S 接受这两个 optional 字段；
            # 当前 device_health 路由仅接受不消费（迁移在 /activity/report 主路径）。
            "hw_fingerprint": self.hw_fingerprint or None,
            "legacy_machine_id": self.legacy_machine_id,
        }


def _iso(dt: datetime | None) -> str | None:
    """与 server 时间口径一致：UTC ISO 8601，秒精度，'Z' 后缀。"""
    if dt is None:
        return None
    return dt.astimezone(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")

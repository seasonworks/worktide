"""主循环：周期性检测空闲时间并上报服务端。"""
import logging
import threading
from pathlib import Path
from typing import Union

import requests

from .config import Config, data_dir
from .health_reporter import HealthReporter
from .idle import get_idle_seconds
from .lifecycle import LifecycleRecorder
from .machine import get_hostname, get_machine_identity
from .reporter import Reporter
from .screenshot import ScreenshotService
from .update_pipeline import UpdatePipeline
from .update_state import UpdateState
from .watchdog import WORKER_AGENT_LOOP, Heartbeat, _NullHeartbeat
from .window_buffer import WindowBuffer
from .window_tracker import WindowTracker
from .window_uploader import WindowUploader

logger = logging.getLogger(__name__)

# Phase 5.2：Heartbeat 与 NullHeartbeat 接口同构，用 Union 接受任一
HeartbeatLike = Union[Heartbeat, _NullHeartbeat]


def _resolve_window_buffer_path(config: Config) -> Path:
    """空字符串 → data_dir()/data/window_buffer.db；否则按 config 给定路径。"""
    p = config.window.buffer_path
    if p:
        return Path(p)
    return data_dir() / "data" / "window_buffer.db"


class Agent:
    def __init__(
        self,
        config: Config,
        *,
        heartbeat: HeartbeatLike | None = None,
        lifecycle: LifecycleRecorder | None = None,
        watchdog: object | None = None,
    ) -> None:
        self.config = config
        # Phase 6.0D · 设备身份硬化：machine.json 缓存 + WMI 硬件指纹 + 旧
        # MachineGuid 迁移线索。详见 client/app/machine.py 模块 docstring。
        self.identity = get_machine_identity()
        self.machine_id = self.identity.machine_id  # 历史字段保留：tests / 日志友好
        self.hostname = get_hostname()
        self.reporter = Reporter(
            config, self.machine_id, self.hostname,
            hw_fingerprint=self.identity.hw_fingerprint,
            legacy_machine_id=self.identity.legacy_machine_id,
        )
        self.screenshots = ScreenshotService(config.screenshot)
        self._stop = threading.Event()
        # 当前是否处于"服务端不可达"状态，用于失败日志去重
        self._offline = False

        # Phase 5.2：heartbeat 默认 NullHeartbeat —— 源码运行 / 单测不强制
        # 起 watchdog 也能跑；main.py 会注入真 Heartbeat 实例。
        self._heartbeat: HeartbeatLike = heartbeat or _NullHeartbeat()
        # Phase 5.3：lifecycle / watchdog 由 main.py 注入；单测 / 简单源码运行
        # 时也可不传，HealthReporter 会优雅降级（lifecycle 用本地默认实例）
        self._lifecycle = lifecycle
        self._watchdog = watchdog

        # Phase 4.1 · 窗口活动管线（与 Reporter / Presence 解耦）。
        # window.enabled=False 时 buffer/tracker/uploader 均为 None，零开销。
        self.window_buffer: WindowBuffer | None = None
        self.window_tracker: WindowTracker | None = None
        self.window_uploader: WindowUploader | None = None
        if config.window.enabled:
            buffer_path = _resolve_window_buffer_path(config)
            self.window_buffer = WindowBuffer(buffer_path)
            self.window_tracker = WindowTracker(
                config.window, self.window_buffer,
                heartbeat=self._heartbeat,
            )
            self.window_uploader = WindowUploader(
                config.window,
                self.window_buffer,
                machine_id=self.machine_id,
                url=config.windows_report_url,
                request_timeout_seconds=config.request_timeout_seconds,
                heartbeat=self._heartbeat,
                # Phase 6.0D · 同一 MachineIdentity 注入到所有 reporter
                hw_fingerprint=self.identity.hw_fingerprint,
                legacy_machine_id=self.identity.legacy_machine_id,
            )

        # Phase 5.4 · update 状态机；与 HealthReporter 共读，update_pipeline 写
        self.update_state = UpdateState()

        # Phase 5.3 · Health 心跳；与 window 管线无关，独立可用
        self.health_reporter: HealthReporter | None = None
        if config.health.enabled:
            # HealthReporter 需要一个 LifecycleRecorder；如果 main.py 没传，
            # 自建一个—两个实例读同一文件，无写并发（main 写一次，reporter 只读）
            lc = self._lifecycle or LifecycleRecorder()
            self.health_reporter = HealthReporter(
                config.health,
                lc,
                machine_id=self.machine_id,
                hostname=self.hostname,
                employee_name=config.employee_name or "",
                url=config.health_url,
                request_timeout_seconds=config.request_timeout_seconds,
                watchdog=self._watchdog,
                window_buffer=self.window_buffer,
                update_state=self.update_state,
                # Phase 6.0D
                hw_fingerprint=self.identity.hw_fingerprint,
                legacy_machine_id=self.identity.legacy_machine_id,
            )

        # Phase 5.4 · Update Pipeline（check → download → stage → install）
        self.update_pipeline: UpdatePipeline | None = None
        if config.update.enabled:
            lc = self._lifecycle or LifecycleRecorder()
            self.update_pipeline = UpdatePipeline(
                config.update,
                self.update_state,
                lc,
                machine_id=self.machine_id,
                server_base_url=config.server_url,
                request_timeout_seconds=max(30, config.request_timeout_seconds * 3),
                # D3 强制：install 前同步上报 installing；reuse health_reporter
                # 的 url + build_payload，避免重复实现
                health_post_url=config.health_url,
                health_post_payload_builder=(
                    self.health_reporter.build_payload
                    if self.health_reporter else None
                ),
            )

    def _on_report_success(self, result: dict, idle: float, is_active: bool) -> None:
        if self._offline:
            logger.info("已恢复与服务端的连接")
            self._offline = False
        logger.info(
            "上报成功 idle=%.0fs active=%s 服务端状态=%s 告警=%s",
            idle,
            is_active,
            result.get("status"),
            result.get("is_idle_alert"),
        )

    def _on_report_failure(self, exc: requests.RequestException) -> None:
        # 首次失败记 WARNING，持续失败降为 DEBUG，避免每个周期刷屏
        if not self._offline:
            logger.warning("上报失败，服务端暂不可达，将持续重试：%s", exc)
            self._offline = True
        else:
            logger.debug("上报仍失败：%s", exc)

    def run_once(self) -> None:
        """执行一次检测 + 上报。

        网络异常在此就地记录（带去重），不中断循环；其他未预期异常
        交由 run() 的兜底处理，保证循环不被任何单次错误终止。
        """
        idle = get_idle_seconds()
        # 最近一个上报周期内有输入即视为活跃
        is_active = idle < self.config.report_interval_seconds

        try:
            result = self.reporter.send(idle, is_active)
        except requests.RequestException as exc:
            self._on_report_failure(exc)
        else:
            self._on_report_success(result, idle, is_active)

        # 预留：未来在此触发截图上传
        self.screenshots.maybe_capture()

    def run(self) -> None:
        # Phase 6.0D 验收 D：明确显示 machine_id / legacy_machine_id / hw_fingerprint。
        # hw_fingerprint 全量打印（32 char），便于运营在多机巡检时直接比对；
        # 它是单向哈希截断，不包含 BIOS / 主板序列号原值。
        logger.info(
            "客户端启动 machine_id=%s legacy_machine_id=%s hw_fingerprint=%s "
            "identity_source=%s host=%s 上报地址=%s 间隔=%ss",
            self.machine_id,
            self.identity.legacy_machine_id or "<none>",
            self.identity.hw_fingerprint or "<unavailable>",
            self.identity.source,
            self.hostname,
            self.config.report_url,
            self.config.report_interval_seconds,
        )
        self._stop.clear()
        self._start_window_pipeline()
        try:
            while not self._stop.is_set():
                # Phase 5.2：每轮顶部 tick，证明 agent 主循环还活着。
                # tick 在 try 外：即便 run_once 抛出，下面的 except 兜底后
                # 还要继续循环，本拍的 tick 已经记下来不影响下一拍。
                self._heartbeat.tick(WORKER_AGENT_LOOP)
                try:
                    self.run_once()
                except Exception:  # noqa: BLE001  单次异常不应终止整个上报循环
                    logger.exception("本次上报周期出现未预期异常，已忽略并继续")
                # 用 Event.wait 替代 sleep，便于被 stop() 立即唤醒
                self._stop.wait(self.config.report_interval_seconds)
        except KeyboardInterrupt:
            logger.info("收到退出信号，停止上报")
        finally:
            self._stop_window_pipeline()
            logger.info("客户端已停止")

    def stop(self) -> None:
        self._stop.set()

    # ------------------------------------------------------------------
    # Phase 4.1 · 窗口活动管线启停
    # ------------------------------------------------------------------

    def _start_window_pipeline(self) -> None:
        """window.enabled=True 时启动 tracker + uploader 两个独立线程。
        Phase 5.3：顺带启动 health reporter（独立于 window 管线）。
        Phase 5.4：顺带启动 update pipeline。"""
        if self.window_tracker:
            self.window_tracker.start()
        if self.window_uploader:
            self.window_uploader.start()
        if self.health_reporter:
            self.health_reporter.start()
        if self.update_pipeline:
            self.update_pipeline.start()

    def _stop_window_pipeline(self) -> None:
        """顺序：先停 tracker（停止采样并 flush 残留 session），再停 uploader。

        这样最后一段 session 也能被 uploader 在后续重启 / 下次启动时续传。
        """
        if self.window_tracker:
            try:
                self.window_tracker.stop()
            except Exception:  # noqa: BLE001
                logger.exception("停止 window tracker 异常")
        if self.window_uploader:
            try:
                self.window_uploader.stop()
            except Exception:  # noqa: BLE001
                logger.exception("停止 window uploader 异常")
        if self.health_reporter:
            try:
                self.health_reporter.stop()
            except Exception:  # noqa: BLE001
                logger.exception("停止 health reporter 异常")
        if self.update_pipeline:
            try:
                self.update_pipeline.stop()
            except Exception:  # noqa: BLE001
                logger.exception("停止 update pipeline 异常")

"""Phase 5.2 · In-process Watchdog + Heartbeat

设计要点（与设计稿 D1-D6 决策一致）：

- **Heartbeat**：内存 ``dict[name → monotonic_ts]``。每个 worker
  （tracker / uploader / agent_loop）在自己的循环顶部调 ``tick(name)``，
  证明这一拍它进入了循环。``tick`` 是 O(1)、加细粒度锁，对热路径几乎零开销。
- **Watchdog**：独立 daemon 线程，每 ``check_interval_seconds`` 醒一次，
  对每个 worker 比较 ``age vs threshold``。
- **D2 / 连续 2 次 miss 才退**：一次抖动（网络重试、SQLite 短锁）不该
  让整个 agent 重启。``miss_count_to_exit=2`` 在默认 60s check 下意味着
  最少 ``threshold + 60`` 秒持续异常才触发，足够过滤瞬时干扰。
- **D4 / 直接 ``os._exit(2)``**：不做 graceful flush。uploader 自身带 retry
  + pending 队列，新 agent 起来后 fetch_pending 续传。daemon 线程里
  ``sys.exit`` 只 raise SystemExit 到本线程，主线程会无视；必须 ``os._exit``
  强制终止整个进程，让 task scheduler 的 ``RestartCount=3`` 接管。
- **D5 / restart_count.json**：lifecycle 模块负责；watchdog 在 ``os._exit``
  之前同步调 ``lifecycle.record_exit(reason)`` 让"为什么挂"落盘。
- **D6 / exit reason 显式化**：reason 命名为 ``watchdog_<worker>_timeout``，
  与 [lifecycle.py] 的 ``EXIT_REASON_WATCHDOG_*`` 常量对齐。
- **D1 / heartbeat.json**：watchdog 每 ``heartbeat_file_throttle_seconds``
  写一次 ``<data_dir>/state/heartbeat.json``，让外部 ``cat`` 即可读健康度。
  写文件失败不影响主循环 —— 那是观察用通道，不是控制路径。

时钟选择：
- **monotonic** 用于 age / threshold 比较（系统时钟跳变不影响 lag）
- **wall clock UTC** 仅用于写 heartbeat.json 的 ISO 字符串，由 anchor
  线性反推（``wall ≈ wall_anchor + (mono - mono_anchor)``）。

阈值说明（默认在 [config.py] ``WatchdogConfig``）：
- tracker = 90s ：期望 ≤ 20s 一次，给 4.5× 容差
- uploader = 360s：upload_backoff 最长 300s + drain timeout 10s ≈ 310s worst case；
  设 360s 留 50s 缓冲。**不要设太小**，否则网络中断时 watchdog 会误杀
- agent_loop = 180s：report_interval=30s + timeout=10s = 40s；6× 容差
"""
from __future__ import annotations

import json
import logging
import os
import threading
import time
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Callable, Optional

from .config import WatchdogConfig, data_dir
from .lifecycle import LifecycleRecorder

logger = logging.getLogger(__name__)


# Worker 名（避免 typo；与 [lifecycle.py] EXIT_REASON_* 后缀对齐）
WORKER_AGENT_LOOP = "agent_loop"
WORKER_TRACKER = "tracker"
WORKER_UPLOADER = "uploader"


# ---------------------------------------------------------------------------
# Heartbeat
# ---------------------------------------------------------------------------

class Heartbeat:
    """Thread-safe last-tick 时间戳表。

    所有 worker 共享同一个实例。读 ``age()`` 与写 ``tick()`` 各加锁 —
    锁极短（dict 单 key 读写），对 tracker 2s 采样这种热路径基本无影响。
    """

    def __init__(self, clock: Callable[[], float] = time.monotonic) -> None:
        self._clock = clock
        self._ticks: dict[str, float] = {}
        self._lock = threading.Lock()
        # 进程"出生时刻"：未 tick 过的 worker，age = now - birth。
        # 这样 watchdog 在 worker 还没第一次 tick 时不会立刻误判（threshold
        # 通常 > check_interval，第一拍内 worker 一定能完成首次 tick）。
        self._birth = clock()

    def tick(self, name: str) -> None:
        ts = self._clock()
        with self._lock:
            self._ticks[name] = ts

    def snapshot(self) -> dict[str, float]:
        """返回 monotonic ts 的 *拷贝*；用于 watchdog 和测试。"""
        with self._lock:
            return dict(self._ticks)

    def birth(self) -> float:
        return self._birth

    def age(self, name: str, now: Optional[float] = None) -> float:
        """``now - last_tick``；从未 tick 的 worker 用 birth 作基准。"""
        if now is None:
            now = self._clock()
        with self._lock:
            ts = self._ticks.get(name, self._birth)
        return max(0.0, now - ts)


class _NullHeartbeat:
    """禁用 watchdog 时的占位实现，调用方代码不必到处 if-None。"""

    def tick(self, name: str) -> None:  # noqa: D401  trivial
        return

    def snapshot(self) -> dict[str, float]:
        return {}

    def birth(self) -> float:
        return 0.0

    def age(self, name: str, now: Optional[float] = None) -> float:
        return 0.0


def make_heartbeat(enabled: bool) -> Heartbeat | _NullHeartbeat:
    """根据配置决定是否启用真心跳；调用方拿到的对象**接口完全相同**。"""
    return Heartbeat() if enabled else _NullHeartbeat()


# ---------------------------------------------------------------------------
# Watchdog
# ---------------------------------------------------------------------------

class Watchdog:
    """Daemon 线程：定期对比 heartbeat age 与阈值，超阈值即退出。

    单元测试入口：``_check_once()`` —— 不启线程也能验证决策；
    系统测试入口：``start()`` + 注入 fake clock + 跳阈值。
    """

    EXIT_CODE = 2

    def __init__(
        self,
        config: WatchdogConfig,
        heartbeat: Heartbeat,
        lifecycle: LifecycleRecorder,
        *,
        heartbeat_file_path: Optional[Path] = None,
        exit_callable: Callable[[int], None] = os._exit,
        clock: Callable[[], float] = time.monotonic,
        wall_clock: Callable[[], float] = time.time,
    ) -> None:
        self._config = config
        self._heartbeat = heartbeat
        self._lifecycle = lifecycle

        self._exit_callable = exit_callable
        self._clock = clock
        self._wall_clock = wall_clock

        # mono ↔ wall 转换的双 anchor，确保 heartbeat.json 里的 ISO 时间
        # 与系统时钟一致，即使 monotonic 与 wall 起点不同也对得上。
        self._mono_anchor = clock()
        self._wall_anchor = wall_clock()

        # 三档阈值（设计稿默认；可被 config 覆盖）
        self._thresholds: dict[str, float] = {
            WORKER_TRACKER: float(config.tracker_threshold_seconds),
            WORKER_UPLOADER: float(config.uploader_threshold_seconds),
            WORKER_AGENT_LOOP: float(config.agent_loop_threshold_seconds),
        }
        self._miss_to_exit = max(1, int(config.miss_count_to_exit))
        self._check_interval = max(1, int(config.check_interval_seconds))
        self._heartbeat_throttle = max(1, int(config.heartbeat_file_throttle_seconds))

        self._heartbeat_file = heartbeat_file_path or (
            data_dir() / "state" / "heartbeat.json"
        )

        self._stop = threading.Event()
        self._thread: Optional[threading.Thread] = None
        # 把 miss 状态作为实例属性而非局部变量，便于 _check_once 单测复用
        self._misses: dict[str, int] = {name: 0 for name in self._thresholds}

    # ------------------------------------------------------------------
    # 线程生命周期
    # ------------------------------------------------------------------

    def start(self) -> None:
        if self._thread and self._thread.is_alive():
            return
        self._stop.clear()
        # daemon=True：主进程退出时 watchdog 跟着死，不留尾巴
        self._thread = threading.Thread(
            target=self._run, name="watchdog", daemon=True
        )
        self._thread.start()
        logger.info(
            "watchdog 启动 check=%ds tracker=%ds uploader=%ds agent_loop=%ds miss=%d",
            self._check_interval,
            int(self._thresholds[WORKER_TRACKER]),
            int(self._thresholds[WORKER_UPLOADER]),
            int(self._thresholds[WORKER_AGENT_LOOP]),
            self._miss_to_exit,
        )

    def stop(self) -> None:
        """优雅停止；单元测试用得着，生产路径中 watchdog 永远跑到进程结束。"""
        self._stop.set()
        if self._thread:
            self._thread.join(timeout=self._check_interval + 2)
        logger.info("watchdog 已停止")

    # ------------------------------------------------------------------
    # Phase 5.3 · 给 HealthReporter 的快照接口
    # ------------------------------------------------------------------

    def snapshot_for_reporting(self) -> dict:
        """供 HealthReporter 上报 watchdog 子结构用。**只读**，零副作用。

        返回的结构与 [server/app/schemas.py] ``HealthWatchdogIn`` 一一对应。
        """
        now = self._clock()
        return {
            "enabled": True,
            "thresholds_seconds": {
                name: int(thr) for name, thr in self._thresholds.items()
            },
            "ages_seconds": {
                name: round(self._heartbeat.age(name, now), 1)
                for name in self._thresholds
            },
            "misses": dict(self._misses),
        }

    # ------------------------------------------------------------------
    # 主循环
    # ------------------------------------------------------------------

    def _run(self) -> None:
        last_write = 0.0
        while not self._stop.is_set():
            # Event.wait：stop() 能立即唤醒，避免单测最多等一个 check_interval
            self._stop.wait(self._check_interval)
            if self._stop.is_set():
                break

            now = self._clock()
            triggered = self._check_once(now)
            if triggered:
                self._trigger_exit(triggered)
                return  # _trigger_exit 调用 os._exit 后这里其实不会执行；保留以防 exit_callable 被 mock

            # 节流写 heartbeat.json（D1 决策；写失败不阻塞主循环）
            if now - last_write >= self._heartbeat_throttle:
                try:
                    self._write_heartbeat_file(now)
                except Exception:  # noqa: BLE001  observability 失败不应中断 watchdog 本身
                    logger.exception("写 heartbeat.json 失败")
                last_write = now

    # ------------------------------------------------------------------
    # 决策（可被单测直接调）
    # ------------------------------------------------------------------

    def _check_once(self, now: float) -> Optional[str]:
        """单轮检查所有 worker。返回**第一个**触发退出的 worker name，
        没有触发返回 None。

        每个 worker 独立计 miss：
        - age > threshold → miss += 1，记 WARNING
        - age 恢复 → miss 归零（"自愈"路径不应触发硬退）
        - miss 累到 ``miss_count_to_exit`` → 返回 worker name 给 _trigger_exit
        """
        triggered: Optional[str] = None
        for name, threshold in self._thresholds.items():
            age = self._heartbeat.age(name, now)
            if age > threshold:
                self._misses[name] += 1
                logger.warning(
                    "watchdog: %s heartbeat stale age=%.1fs threshold=%.1fs miss=%d/%d",
                    name, age, threshold, self._misses[name], self._miss_to_exit,
                )
                if self._misses[name] >= self._miss_to_exit and triggered is None:
                    # 取第一个触发，break 让 dict 顺序成为优先级
                    triggered = name
            else:
                # 阈值内 → miss 归零，"接近超时但又恢复"不算累积
                if self._misses[name] > 0:
                    logger.info(
                        "watchdog: %s heartbeat recovered age=%.1fs (miss was %d)",
                        name, age, self._misses[name],
                    )
                self._misses[name] = 0
        return triggered

    # ------------------------------------------------------------------
    # 退出 & 落盘
    # ------------------------------------------------------------------

    def _trigger_exit(self, worker: str) -> None:
        """D6：reason 命名为 ``watchdog_<worker>_timeout``。

        顺序：先落盘 last_exit_reason → 再 os._exit。如果 lifecycle 写失败，
        仍要硬退 —— 因为不退就意味着 agent 处于无法工作的状态，
        task scheduler 的 RestartCount 永远不会接管。
        """
        reason = f"watchdog_{worker}_timeout"
        logger.error(
            "watchdog triggered exit: %s (misses=%s)",
            reason, self._misses,
        )
        try:
            self._lifecycle.record_exit(reason)
        except Exception:  # noqa: BLE001  落盘失败不应阻止硬退
            logger.exception("record_exit 失败，仍继续硬退")
        # os._exit 立即终止，跳过 finally / atexit / threading shutdown。
        # 这是有意为之 —— graceful 路径已经被证明会卡住，hard exit 是最后兜底。
        self._exit_callable(self.EXIT_CODE)

    # ------------------------------------------------------------------
    # heartbeat.json 落盘（观察通道）
    # ------------------------------------------------------------------

    def _mono_to_iso(self, mono: float) -> str:
        """monotonic ts → UTC ISO 字符串，用双 anchor 线性反推。

        wall ≈ wall_anchor + (mono - mono_anchor)
        """
        wall_unix = self._wall_anchor + (mono - self._mono_anchor)
        return datetime.fromtimestamp(wall_unix, timezone.utc).strftime(
            "%Y-%m-%dT%H:%M:%SZ"
        )

    def _write_heartbeat_file(self, now: float) -> None:
        """原子写 heartbeat.json。"""
        ticks = self._heartbeat.snapshot()
        payload = {
            "version": "0.5.2",
            "process_started_at": self._mono_to_iso(self._heartbeat.birth())
            if hasattr(self._heartbeat, "birth")
            else None,
            "last_check_at": self._mono_to_iso(now),
            "uptime_seconds": round(now - self._mono_anchor, 1),
            "thresholds_seconds": {
                name: int(thr) for name, thr in self._thresholds.items()
            },
            "miss_count_to_exit": self._miss_to_exit,
            "ages_seconds": {
                name: round(self._heartbeat.age(name, now), 1)
                for name in self._thresholds
            },
            "ticks_iso": {
                name: self._mono_to_iso(ts) for name, ts in ticks.items()
            },
            "misses": dict(self._misses),
        }

        # 确保父目录存在（state/ 应已由 lifecycle 创建，但本模块独立可用）
        try:
            self._heartbeat_file.parent.mkdir(parents=True, exist_ok=True)
        except OSError:
            pass

        tmp = self._heartbeat_file.with_suffix(
            self._heartbeat_file.suffix + ".tmp"
        )
        try:
            tmp.write_text(
                json.dumps(payload, ensure_ascii=False, indent=2, sort_keys=True),
                encoding="utf-8",
            )
            os.replace(tmp, self._heartbeat_file)
        except OSError as e:
            logger.warning("heartbeat.json 写失败 (path=%s)：%s", self._heartbeat_file, e)
            try:
                if tmp.exists():
                    tmp.unlink()
            except OSError:
                pass

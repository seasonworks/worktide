"""客户端窗口活动 Uploader（Phase 4.1 Step 4C）。

只做三件事：
1. 从 WindowBuffer 拉 pending 事件
2. 批量 POST 到 /api/v1/windows/report
3. 根据响应：200 → mark_uploaded；422 → bisect/quarantine；其它 → 退避重试

明确不做：
- 采样 / session 合并 / merge / analytics / 时区换算
- 写 invalid 行的二次处理（quarantine 一锤定音，留作人工审计）

状态机：
    pending --POST 200--> uploaded
    pending --POST 422 (单条)--> attempts++ --(>= max_attempts)--> invalid
    pending --POST 422 (多条)--> 二分递归
    pending --timeout / 5xx / 4xx-非-422 / 网络错误--> 不变，退避后重试

退避：upload_backoff_seconds = [5, 30, 120, 300]，封顶 300s。
首次拉到 0 条 / 一次完整 drain 成功 → backoff_idx 归零。

隐私红线：process_name / window_title **绝不**进入 INFO/WARNING 日志；
DEBUG 也只输出 count / event_id / status，不输出可识别内容。
"""
from __future__ import annotations

import logging
import threading
from typing import Optional

import requests

from .config import WindowTrackingConfig
from .window_buffer import WindowBuffer

logger = logging.getLogger(__name__)


# ---------------------------------------------------------------------------
# 内部异常：用于把"瞬时不可达"统一为一个通道，让 drain 决定退避
# ---------------------------------------------------------------------------

class _NetworkError(Exception):
    """非 200 / 非 422 的所有情形（timeout / 5xx / 4xx-非-422 / 网络错误）。

    语义：本次请求**没有可信结论**，绝不允许 mark_uploaded，也不可增 attempts。
    上层应：保留 pending，按退避重试。
    """


# ---------------------------------------------------------------------------
# Uploader
# ---------------------------------------------------------------------------

class WindowUploader:
    """周期 drain buffer 并上报。线程模型与 Reporter 同构（自带 daemon 线程）。

    依赖注入（便于测试）：
        session    可注入 fake requests.Session；默认新建 requests.Session
        clock      仅供未来扩展；目前不依赖时钟，由 buffer.uploaded_at 自动写
    """

    def __init__(
        self,
        config: WindowTrackingConfig,
        buffer: WindowBuffer,
        *,
        machine_id: str,
        url: str,
        request_timeout_seconds: int = 10,
        session: Optional[requests.Session] = None,
        heartbeat: object = None,
        # Phase 6.0D · Device Identity Hardening；老调用方（测试）不传 → 默认空
        hw_fingerprint: str = "",
        legacy_machine_id: Optional[str] = None,
    ) -> None:
        self.config = config
        self.buffer = buffer
        self.machine_id = machine_id
        self.url = url
        self.timeout = max(1, int(request_timeout_seconds))
        self.session = session or requests.Session()
        self.hw_fingerprint = hw_fingerprint
        self.legacy_machine_id = legacy_machine_id

        self._stop = threading.Event()
        self._thread: Optional[threading.Thread] = None

        # Phase 5.2：duck typing + 延迟 import 避环（同 window_tracker）
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
            target=self._run, name="window-uploader", daemon=True
        )
        self._thread.start()
        logger.info(
            "window uploader 启动 url=%s batch=%d interval=%ds max_attempts=%d",
            self.url,
            self.config.upload_batch_size,
            self.config.upload_interval_seconds,
            self.config.upload_max_attempts,
        )

    def stop(self) -> None:
        self._stop.set()
        if self._thread:
            self._thread.join(timeout=10)
        logger.info("window uploader 已停止")

    # ------------------------------------------------------------------
    # 主循环
    # ------------------------------------------------------------------

    def _run(self) -> None:
        from .watchdog import WORKER_UPLOADER  # 延迟 import 避环

        backoff = self._backoff_list()
        backoff_idx = 0
        # uploader 不持续 drain 到底：每周期最多 drain 一次 batch，
        # 让 tracker 有节奏地产 event；积压时下一周期继续 drain。
        # 例外：本周期成功且 fetch=batch_size（满批）时立即续 drain，避免堆积。
        while not self._stop.is_set():
            # Phase 5.2：每拍顶部 heartbeat。注意 backoff 路径下面也有 wait，
            # 但循环回头还会先到这里 tick；watchdog uploader_threshold=360s
            # 已经覆盖最长 backoff 300s + drain timeout 的 worst case。
            self._heartbeat.tick(WORKER_UPLOADER)
            try:
                stats = self.drain_once()
            except Exception:  # noqa: BLE001  保护线程不被未预期异常打死
                logger.exception("uploader drain 出现未预期异常")
                stats = {"network_error": True, "fetched": 0}

            if stats.get("network_error"):
                wait = backoff[min(backoff_idx, len(backoff) - 1)]
                logger.warning(
                    "upload 失败，retry in %ds（reason=%s）",
                    wait,
                    stats.get("reason", "unknown"),
                )
                backoff_idx += 1
                self._stop.wait(wait)
                continue

            # 成功（含 0 条）→ 退避归零
            backoff_idx = 0

            # 满批继续 drain，避免积压；否则按周期 sleep
            if stats.get("fetched", 0) >= self.config.upload_batch_size:
                # 即刻续 drain，不睡眠
                continue
            self._stop.wait(self.config.upload_interval_seconds)

    # ------------------------------------------------------------------
    # drain：单次拉一批，处理结果
    # ------------------------------------------------------------------

    def drain_once(self) -> dict:
        """单次拉 pending 并上传；返回计数字典。可被测试直接调用。

        计数键：
            fetched           本次拉到的 pending 数
            uploaded          成功 mark_uploaded 的数（含服务端 deduped 部分）
            deduped           服务端返回的 deduped（属于 uploaded 子集，仅供日志）
            attempts_added    422 单条触发 increment_attempt 的数
            quarantined       attempts>=max 后 quarantine 的数
            sub_batches       _upload_chunk 实际触发的 POST 次数
            network_error     bool；True 表示本轮要求外层退避
            reason            网络错误简述
        """
        rows = self.buffer.fetch_pending(self.config.upload_batch_size)
        stats = _empty_stats()
        stats["fetched"] = len(rows)
        if not rows:
            return stats

        try:
            self._upload_chunk(rows, stats)
        except _NetworkError as exc:
            stats["network_error"] = True
            stats["reason"] = str(exc)
            return stats

        # 隐私：不输出 process_name / window_title
        if stats["uploaded"] or stats["quarantined"] or stats["attempts_added"]:
            logger.info(
                "uploaded %d events (deduped=%d, attempts_added=%d, "
                "quarantined=%d, sub_batches=%d)",
                stats["uploaded"],
                stats["deduped"],
                stats["attempts_added"],
                stats["quarantined"],
                stats["sub_batches"],
            )
        return stats

    # ------------------------------------------------------------------
    # 递归 chunk：200 即 mark；422 二分；其它抛 NetworkError
    # ------------------------------------------------------------------

    def _upload_chunk(self, rows: list[dict], stats: dict) -> None:
        """对一批 rows 做 POST 决策。stats 原地累加。

        递归出口：
            len(rows)==0 → 直接返回
            HTTP 200     → mark_uploaded(全部 ids)，累加 uploaded/deduped
            HTTP 422 单条 → increment_attempt；若 >=max_attempts → quarantine
            HTTP 422 多条 → 二分递归（左右半段）
            其它          → raise _NetworkError
        """
        if not rows:
            return

        stats["sub_batches"] += 1
        try:
            resp = self.session.post(
                self.url, json=self._payload(rows), timeout=self.timeout
            )
        except requests.RequestException as exc:
            # 超时 / 连接拒绝 / DNS / SSL 等
            raise _NetworkError(f"requests: {exc.__class__.__name__}") from exc

        code = resp.status_code

        # ---- 200 OK：批内全部 mark_uploaded ----
        if code == 200:
            ids = [r["client_event_id"] for r in rows]
            n = self.buffer.mark_uploaded(ids)
            stats["uploaded"] += n
            # body 失败不算上传失败：服务端已 ACK，本地已 mark
            try:
                body = resp.json()
                deduped = int(body.get("deduped", 0))
                stats["deduped"] += deduped
            except (ValueError, TypeError):
                pass
            return

        # ---- 422 Unprocessable Entity：可能批内有坏数据 ----
        if code == 422:
            if len(rows) == 1:
                self._handle_single_422(rows[0], stats)
                return
            mid = len(rows) // 2
            # 左右独立递归；左成功的 mark_uploaded 已在内部完成，
            # 右抛 NetworkError 也不回滚左侧的 mark（mark 单调）
            self._upload_chunk(rows[:mid], stats)
            self._upload_chunk(rows[mid:], stats)
            return

        # ---- 其它：4xx-非-422 / 5xx → 视为瞬时不可达 ----
        # 例如 400 machine_id 未注册：等管理员注册后下次再传；不增 attempts。
        raise _NetworkError(f"HTTP {code}")

    def _handle_single_422(self, row: dict, stats: dict) -> None:
        """单条 422：increment_attempt；攒满 max_attempts 后 quarantine。"""
        event_id = row["client_event_id"]
        attempts = self.buffer.increment_attempt(event_id)
        stats["attempts_added"] += 1
        if attempts >= self.config.upload_max_attempts:
            self.buffer.quarantine_event(
                event_id,
                reason=f"422 x{attempts}",
            )
            stats["quarantined"] += 1
            # buffer.quarantine_event 已经记 WARNING：
            #   "buffer quarantine: event_id=%s reason=%s"
            # 这里不重复记录，避免日志冗余

    # ------------------------------------------------------------------
    # 工具
    # ------------------------------------------------------------------

    def _payload(self, rows: list[dict]) -> dict:
        return {
            "machine_id": self.machine_id,
            "events": [_row_to_event(r) for r in rows],
            # Phase 6.0D · server 6.0D-S 已接受这两个 optional 字段；
            # 当前路由仅接受不消费（迁移在 /activity/report 主路径完成），
            # 这里附带以便未来 server 端做窗口侧的克隆侦测扩展。
            "hw_fingerprint": self.hw_fingerprint or None,
            "legacy_machine_id": self.legacy_machine_id,
        }

    def _backoff_list(self) -> list[int]:
        cfg = list(self.config.upload_backoff_seconds or [])
        # 容错：空配置时退化为 30s 固定退避
        return cfg if cfg else [30]


# ---------------------------------------------------------------------------
# 纯函数辅助（无副作用，便于测试）
# ---------------------------------------------------------------------------

def _row_to_event(r: dict) -> dict:
    """把 WindowBuffer.fetch_pending() 的字典裁剪为服务端 WindowEventIn 形态。

    剔除本地字段：id / upload_attempts。
    """
    return {
        "client_event_id": r["client_event_id"],
        "process_name": r["process_name"],
        "window_title": r.get("window_title", ""),
        "started_at": r["started_at"],
        "ended_at": r["ended_at"],
        "duration_seconds": int(r["duration_seconds"]),
        "had_input": bool(r["had_input"]),
    }


def _empty_stats() -> dict:
    return {
        "fetched": 0,
        "uploaded": 0,
        "deduped": 0,
        "attempts_added": 0,
        "quarantined": 0,
        "sub_batches": 0,
        "network_error": False,
        "reason": "",
    }

"""Phase 5.2 · 进程生命周期落盘 (``restart_count.json``)

记录 agent 每次启动 / 退出，让运维和 watchdog 自身可见：
- 进程重启了多少次
- 上次为什么退出（normal / watchdog_*_timeout / crash / initial）
- 第一次启动时间、最近一次启动时间

文件路径：``<data_dir>/state/restart_count.json``

字段 schema::

    {
      "version": "0.5.2",
      "restart_count": 7,
      "first_start_at": "2026-05-26T20:00:00Z",
      "last_start_at":  "2026-05-30T15:00:00Z",
      "last_exit_at":   "2026-05-30T14:59:30Z",   # 可能为 null（从未退过）
      "last_exit_reason": "watchdog_tracker_timeout"
    }

写入策略：**原子写**（写 tmp + ``os.replace``），避免外部脚本读到半截 JSON。
读取失败（损坏 / 缺失）→ 当作空记录，从 0 重新累加；这里我们**不**回退到崩溃，
因为 lifecycle 是 *观察用*，不应影响 agent 正常启动。

与 [watchdog.py] 的关系：watchdog 在 ``os._exit`` 前调用 ``record_exit(reason)``，
把退出原因落盘后再硬退；main.py 的顶层异常 handler 也调用 ``record_exit("crash")``。
"""
from __future__ import annotations

import json
import logging
import os
from datetime import datetime, timezone
from pathlib import Path
from typing import Optional

from .config import data_dir

logger = logging.getLogger(__name__)

#: 与 watchdog 共用的 schema 版本号；改 schema 时同步 bump
STATE_VERSION = "0.5.2"

# Phase 5.2 决策 D6 文档化：退出原因的可枚举集
EXIT_REASON_INITIAL = "initial"      # 从未退出过（首次启动）
EXIT_REASON_NORMAL = "normal"        # main() 正常返回
EXIT_REASON_CRASH = "crash"          # main() 顶层异常
EXIT_REASON_WATCHDOG_TRACKER = "watchdog_tracker_timeout"
EXIT_REASON_WATCHDOG_UPLOADER = "watchdog_uploader_timeout"
EXIT_REASON_WATCHDOG_AGENT_LOOP = "watchdog_agent_loop_timeout"
# Phase 5.4 · agent 主动 exit 让 updater 接管
EXIT_REASON_UPDATE_PENDING = "update_pending"


def _state_dir() -> Path:
    """``<data_dir>/state``；创建失败时退回 data_dir 根部，永不抛错。"""
    d = data_dir() / "state"
    try:
        d.mkdir(parents=True, exist_ok=True)
    except OSError as e:
        logger.warning("无法创建 state 目录 %s：%s，退回 data_dir 根", d, e)
        return data_dir()
    return d


def _now_iso() -> str:
    """UTC ISO 8601，秒精度，'Z' 后缀（与 server 端时间口径一致）。"""
    return datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


class LifecycleRecorder:
    """读写 ``restart_count.json``。

    线程模型：
    - ``record_start()`` —— main.py 在 setup 阶段调用 *一次*
    - ``record_exit(reason)`` —— watchdog 在 ``os._exit`` 前调用，或
      main.py 的异常 handler 调用，或 normal 退出路径调用

    并发：理论上一个进程内不会并发调用。但 watchdog daemon 线程和
    main 线程**可能同时**走到 record_exit —— 例如 watchdog 决定退出的同时
    main 也抛了致命异常。我们用 ``os.replace`` 原子写避免文件被撕裂；
    哪个写入"赢"取决于调度，两个原因都是真实的，**最后一个落盘即可**。
    """

    DEFAULT_FILE_NAME = "restart_count.json"

    def __init__(self, path: Optional[Path] = None) -> None:
        self.path = path or (_state_dir() / self.DEFAULT_FILE_NAME)

    # ------------------------------------------------------------------
    # IO
    # ------------------------------------------------------------------

    def _read(self) -> dict:
        if not self.path.exists():
            return {}
        try:
            return json.loads(self.path.read_text(encoding="utf-8"))
        except Exception:  # noqa: BLE001  损坏 / 编码异常都视同空
            logger.warning(
                "restart_count.json 读取失败，按空记录处理 (path=%s)", self.path
            )
            return {}

    def _write(self, payload: dict) -> None:
        """原子写：先写 .tmp，再 ``os.replace`` 一次性切换。"""
        tmp = self.path.with_suffix(self.path.suffix + ".tmp")
        try:
            tmp.write_text(
                json.dumps(payload, ensure_ascii=False, indent=2, sort_keys=True),
                encoding="utf-8",
            )
            # Windows os.replace 在 same-volume 内是原子的；跨卷会回退到 rename+copy
            os.replace(tmp, self.path)
        except OSError as e:
            logger.warning(
                "restart_count.json 写入失败 (path=%s)：%s", self.path, e
            )
            # 清理 .tmp 避免遗留
            try:
                if tmp.exists():
                    tmp.unlink()
            except OSError:
                pass

    # ------------------------------------------------------------------
    # 业务接口
    # ------------------------------------------------------------------

    def record_start(self) -> dict:
        """累加 restart_count + 刷新 last_start_at。

        **不动** ``last_exit_at`` / ``last_exit_reason``：那是上次进程退出
        时写下的，本次启动的人应当能从这里看到"上次为什么挂"。

        :return: 写入的最新 payload，便于调用方 log
        """
        old = self._read()
        now = _now_iso()
        payload = {
            "version": STATE_VERSION,
            "restart_count": int(old.get("restart_count", 0)) + 1,
            "first_start_at": old.get("first_start_at") or now,
            "last_start_at": now,
            # 保留上次退出信息（如果有）；首次启动时 last_exit_reason 默认为 "initial"
            "last_exit_at": old.get("last_exit_at"),
            "last_exit_reason": old.get("last_exit_reason") or EXIT_REASON_INITIAL,
        }
        self._write(payload)
        return payload

    def snapshot(self) -> dict:
        """读取磁盘上的最新 lifecycle 快照。**只读**，供 HealthReporter 用。

        与 ``record_start`` 返回的 dict 同 schema；区别在于 snapshot 反映
        当前文件状态（可能是别的进程刚写的），record_start 返回的是本进程
        刚写完的状态。
        """
        return self._read()

    def record_exit(self, reason: str) -> None:
        """记录退出原因 + 时间。

        和 ``record_start`` 的字段保留策略对称：保留 ``restart_count`` /
        ``first_start_at`` / ``last_start_at`` 不动，只覆盖 exit 二字段。
        """
        old = self._read()
        now = _now_iso()
        payload = dict(old)
        payload["last_exit_at"] = now
        payload["last_exit_reason"] = reason
        # 兜底：万一 old 完全为空（record_start 没跑过），补齐 schema
        payload.setdefault("version", STATE_VERSION)
        payload.setdefault("restart_count", 0)
        payload.setdefault("first_start_at", now)
        payload.setdefault("last_start_at", now)
        self._write(payload)

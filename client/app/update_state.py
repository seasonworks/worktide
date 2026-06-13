"""Phase 5.4 · update 状态机 + 共享对象。

设计 D3 状态流::

    idle → checking → downloading → staged → installing → idle

任何节点失败 → ``failed`` + last_error 记录原因，下次 check 周期重新进入 idle。

线程模型：
- ``UpdateChecker`` / ``UpdateDownloader`` / ``UpdateInstaller`` 都在
  **同一个** UpdatePipeline 线程里跑（不同 worker 不并发），由
  ``UpdateState`` 串行化状态切换。
- ``HealthReporter`` 在另一线程读 ``UpdateState.snapshot()`` 拼到 payload，
  对状态机只读不写。

为什么不引第三方 statemachine 库：状态机只 6 档，转换矩阵小；自己写 dict
查表比加依赖清晰。
"""
from __future__ import annotations

import logging
import threading
from datetime import datetime, timezone
from typing import Optional

logger = logging.getLogger(__name__)

# 状态枚举（与 server schemas.HealthUpdateIn.status 严格对齐）
IDLE = "idle"
CHECKING = "checking"
DOWNLOADING = "downloading"
STAGED = "staged"
INSTALLING = "installing"
FAILED = "failed"

ALL_STATUSES = {IDLE, CHECKING, DOWNLOADING, STAGED, INSTALLING, FAILED}

# 合法转换矩阵。未列出的转换会被 transition() 拒绝并 log WARNING。
# 任何状态都能转 FAILED（错误路径兜底）。
_TRANSITIONS: dict[str, set[str]] = {
    IDLE:        {CHECKING, FAILED},
    CHECKING:    {DOWNLOADING, IDLE, FAILED},
    DOWNLOADING: {STAGED, FAILED},
    STAGED:      {INSTALLING, FAILED},
    INSTALLING:  {IDLE, FAILED},  # IDLE = 新 agent 起来后重置
    FAILED:      {IDLE, CHECKING},  # 下次 check 周期可重新尝试
}


class UpdateState:
    """线程安全的 update 状态共享对象。

    与 [watchdog.py] ``Heartbeat`` 同思路：一处持有真值，多线程读写都过细粒度锁。
    """

    def __init__(self) -> None:
        self._lock = threading.Lock()
        self._status: str = IDLE
        self._target_version: Optional[str] = None
        self._last_check_at: Optional[datetime] = None
        self._last_error: Optional[str] = None

    # ------------------------------------------------------------------
    # 转换
    # ------------------------------------------------------------------

    def transition(self, new_status: str, *, target_version: Optional[str] = None,
                   error: Optional[str] = None) -> bool:
        """尝试切到 ``new_status``。

        :param target_version: 仅 CHECKING→DOWNLOADING / STAGED 等需要时传
        :param error: 仅 transition_to(FAILED) 时传，会写入 last_error
        :return: ``True`` = 转换成功；``False`` = 非法转换（已记 WARNING）
        """
        if new_status not in ALL_STATUSES:
            logger.warning("update_state: unknown status %r", new_status)
            return False
        with self._lock:
            allowed = _TRANSITIONS.get(self._status, set())
            if new_status not in allowed and new_status != self._status:
                logger.warning(
                    "update_state: illegal %s → %s", self._status, new_status
                )
                return False
            old = self._status
            self._status = new_status
            if target_version is not None:
                self._target_version = target_version
            if new_status == IDLE:
                # 回到 idle 时清理目标版本与错误信息
                self._target_version = None
                self._last_error = None
            if new_status == FAILED:
                self._last_error = (error or "unknown")[:256]
            logger.info(
                "update_state: %s → %s target=%s err=%s",
                old, new_status, self._target_version, self._last_error,
            )
            return True

    def mark_check_attempted(self) -> None:
        """无论 check 成功失败，都更新 last_check_at 时间戳。"""
        with self._lock:
            self._last_check_at = datetime.now(timezone.utc)

    # ------------------------------------------------------------------
    # 只读快照（供 HealthReporter / 测试用）
    # ------------------------------------------------------------------

    @property
    def status(self) -> str:
        with self._lock:
            return self._status

    def snapshot(self) -> dict:
        """构造 HealthReportIn.update 子结构 dict。

        返回包含 ISO 8601 字符串的 last_check_at（与 server schema 对齐）；
        其它字段直接来自内部状态。
        """
        with self._lock:
            return {
                "status": self._status,
                "target_version": self._target_version,
                "last_check_at": (
                    self._last_check_at.strftime("%Y-%m-%dT%H:%M:%SZ")
                    if self._last_check_at else None
                ),
                "last_error": self._last_error,
            }

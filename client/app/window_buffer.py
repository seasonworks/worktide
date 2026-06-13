"""客户端窗口活动 Buffer（SQLite WAL）。

Phase 4.1 Step 4A：**Buffer First**。未来 tracker (4B) 与 uploader (4C) 都只能通过
本模块与本地 SQLite 交互；本模块**不联网、不采样**。

Schema：
    window_events(
        id INTEGER PK,
        client_event_id TEXT UNIQUE NOT NULL,   -- UUID v4，幂等键
        process_name TEXT NOT NULL,
        window_title TEXT NOT NULL DEFAULT '',
        started_at TEXT NOT NULL,                -- ISO 8601 UTC
        ended_at TEXT NOT NULL,
        duration_seconds INTEGER NOT NULL,
        had_input INTEGER NOT NULL DEFAULT 1,
        status TEXT NOT NULL DEFAULT 'pending',  -- pending / uploaded / invalid
        uploaded_at TEXT,                        -- 仅 status=uploaded 时设置
        upload_attempts INTEGER NOT NULL DEFAULT 0,
        created_at TEXT NOT NULL DEFAULT (datetime('now'))
    )

状态机：
    pending  ── mark_uploaded()      → uploaded
             ── quarantine_event()   → invalid

    uploaded ── cleanup_uploaded()   → DELETE（超过 retention 时）
             ── enforce_max_rows()   → DELETE（超过总行上限时；仅删 uploaded）
    invalid  ── 永不自动删除（审计用，保留待人工分析）
    pending  ── 永不被清理函数删除（避免数据丢失）

WAL：每连接 connect 时设置 journal_mode=WAL / synchronous=NORMAL / busy_timeout=5000。
线程模型：每方法独立 connection；SQLite WAL 模式下读写并发安全，不需要外部锁。

隐私：所有 INFO/WARNING 级日志**绝不**输出 process_name / window_title；只输出计数 /
event_id / 状态。
"""
import logging
import sqlite3
from collections.abc import Iterable
from pathlib import Path

logger = logging.getLogger(__name__)

# 状态常量（避免魔法字符串散落各处）
STATUS_PENDING = "pending"
STATUS_UPLOADED = "uploaded"
STATUS_INVALID = "invalid"

_VALID_STATUSES = (STATUS_PENDING, STATUS_UPLOADED, STATUS_INVALID)

_SCHEMA_SQL = """
CREATE TABLE IF NOT EXISTS window_events (
    id               INTEGER PRIMARY KEY AUTOINCREMENT,
    client_event_id  TEXT    NOT NULL UNIQUE,
    process_name     TEXT    NOT NULL,
    window_title     TEXT    NOT NULL DEFAULT '',
    started_at       TEXT    NOT NULL,
    ended_at         TEXT    NOT NULL,
    duration_seconds INTEGER NOT NULL,
    had_input        INTEGER NOT NULL DEFAULT 1,
    status           TEXT    NOT NULL DEFAULT 'pending',
    uploaded_at      TEXT,
    upload_attempts  INTEGER NOT NULL DEFAULT 0,
    created_at       TEXT    NOT NULL DEFAULT (datetime('now'))
);
"""

_INDEX_SQL = (
    "CREATE INDEX IF NOT EXISTS idx_status_id ON window_events(status, id);",
    "CREATE INDEX IF NOT EXISTS idx_uploaded_at ON window_events(uploaded_at);",
)


class WindowBuffer:
    """SQLite WAL Buffer。tracker 写、uploader 读、cleanup 收尾，三方各自独立。

    所有方法都是事务安全的：每次 commit 后才返回，调用方拿到 True/计数即代表
    数据已落盘（WAL 模式下 synchronous=NORMAL 保证 fsync 量级一致性）。
    """

    def __init__(self, path: str | Path) -> None:
        self.path = Path(path)
        self._init_db()

    def _connect(self) -> sqlite3.Connection:
        """每次新建一个连接；WAL 模式下多连接并发读写安全。"""
        conn = sqlite3.connect(str(self.path), timeout=5)
        conn.execute("PRAGMA journal_mode=WAL")
        conn.execute("PRAGMA synchronous=NORMAL")
        conn.execute("PRAGMA busy_timeout=5000")
        return conn

    def _init_db(self) -> None:
        """建表 + 索引（幂等）；父目录不存在则创建。"""
        self.path.parent.mkdir(parents=True, exist_ok=True)
        with self._connect() as conn:
            conn.executescript(_SCHEMA_SQL)
            for sql in _INDEX_SQL:
                conn.execute(sql)
            conn.commit()
        logger.info("window buffer initialized: %s", self.path)

    # ---------------------------------------------------------------------
    # 写入
    # ---------------------------------------------------------------------

    def append_event(
        self,
        client_event_id: str,
        process_name: str,
        window_title: str,
        started_at: str,
        ended_at: str,
        duration_seconds: int,
        had_input: bool = True,
    ) -> bool:
        """追加一条事件到 buffer。client_event_id UNIQUE 冲突视为重复，静默忽略。

        timestamps 字符串需是 ISO 8601 UTC（"YYYY-MM-DDTHH:MM:SSZ"），调用方负责。
        """
        try:
            with self._connect() as conn:
                conn.execute(
                    "INSERT INTO window_events ("
                    "client_event_id, process_name, window_title, "
                    "started_at, ended_at, duration_seconds, had_input"
                    ") VALUES (?, ?, ?, ?, ?, ?, ?)",
                    (
                        client_event_id,
                        process_name,
                        window_title,
                        started_at,
                        ended_at,
                        int(duration_seconds),
                        1 if had_input else 0,
                    ),
                )
                conn.commit()
            return True
        except sqlite3.IntegrityError:
            # UUID 撞库概率忽略；防御性记 DEBUG（不出 process_name / title）
            logger.debug("append_event: client_event_id 重复，跳过")
            return False

    # ---------------------------------------------------------------------
    # 读取
    # ---------------------------------------------------------------------

    def fetch_pending(self, limit: int) -> list[dict]:
        """读取 status=pending 的事件，按 id 升序最多 limit 条。

        返回 list[dict]，键：id / client_event_id / process_name / window_title /
        started_at / ended_at / duration_seconds / had_input / upload_attempts。
        """
        if limit <= 0:
            return []
        with self._connect() as conn:
            cur = conn.execute(
                "SELECT id, client_event_id, process_name, window_title, "
                "started_at, ended_at, duration_seconds, had_input, upload_attempts "
                "FROM window_events WHERE status = ? "
                "ORDER BY id LIMIT ?",
                (STATUS_PENDING, int(limit)),
            )
            cols = [c[0] for c in cur.description]
            rows = [dict(zip(cols, r)) for r in cur.fetchall()]
        for r in rows:
            r["had_input"] = bool(r["had_input"])
        return rows

    # ---------------------------------------------------------------------
    # 状态变更
    # ---------------------------------------------------------------------

    def mark_uploaded(self, client_event_ids: Iterable[str]) -> int:
        """把指定 client_event_id 的 pending 行 → uploaded。返回更新行数。

        仅作用于 pending 行（已 uploaded / invalid 的行不会被改回）。
        """
        ids = list(client_event_ids)
        if not ids:
            return 0
        placeholders = ",".join("?" * len(ids))
        with self._connect() as conn:
            cur = conn.execute(
                f"UPDATE window_events SET status = ?, "
                f"uploaded_at = datetime('now') "
                f"WHERE status = ? AND client_event_id IN ({placeholders})",
                (STATUS_UPLOADED, STATUS_PENDING, *ids),
            )
            conn.commit()
            n = cur.rowcount
        if n:
            logger.debug("mark_uploaded: %d rows", n)
        return n

    def quarantine_event(self, client_event_id: str, reason: str = "") -> bool:
        """把单条事件移入 invalid 隔离区（审计用，**永不**自动删除）。

        典型用法：uploader 连续 N 次 422 后调用，防止坏数据无限重试。
        reason 仅日志使用，不含 title / process_name。
        """
        with self._connect() as conn:
            cur = conn.execute(
                "UPDATE window_events SET status = ?, "
                "upload_attempts = upload_attempts + 1 "
                "WHERE client_event_id = ?",
                (STATUS_INVALID, client_event_id),
            )
            conn.commit()
            updated = cur.rowcount > 0
        if updated:
            logger.warning(
                "buffer quarantine: event_id=%s reason=%s",
                client_event_id,
                reason or "(unspecified)",
            )
        return updated

    def increment_attempt(self, client_event_id: str) -> int:
        """upload_attempts += 1，返回更新后值（不变状态）。

        uploader 用于跨重启持久化失败次数：连续若干次失败后再 quarantine_event。
        """
        with self._connect() as conn:
            cur = conn.execute(
                "UPDATE window_events SET upload_attempts = upload_attempts + 1 "
                "WHERE client_event_id = ? RETURNING upload_attempts",
                (client_event_id,),
            )
            row = cur.fetchone()
            conn.commit()
        return row[0] if row else 0

    # ---------------------------------------------------------------------
    # 清理
    # ---------------------------------------------------------------------

    def cleanup_uploaded(self, retention_hours: int) -> int:
        """删除 status=uploaded 且 uploaded_at < now - retention_hours 的行。

        **绝不**触及 pending 与 invalid。
        """
        if retention_hours <= 0:
            return 0
        with self._connect() as conn:
            cur = conn.execute(
                "DELETE FROM window_events "
                "WHERE status = ? AND uploaded_at IS NOT NULL "
                "AND uploaded_at < datetime('now', ?)",
                (STATUS_UPLOADED, f"-{int(retention_hours)} hours"),
            )
            conn.commit()
            n = cur.rowcount
        if n:
            logger.info("cleanup uploaded rows: deleted %d", n)
        return n

    def enforce_max_rows(self, max_rows: int) -> int:
        """总行数 > max_rows 时，删除最旧的 uploaded 行；返回删除数。

        **绝不**删除 pending 或 invalid。若 pending+invalid 已 ≥ max_rows，
        本函数无能为力，记 WARNING 接受超限（不应误删可能仍需上传的数据）。
        """
        if max_rows <= 0:
            return 0
        with self._connect() as conn:
            (total,) = conn.execute(
                "SELECT COUNT(*) FROM window_events"
            ).fetchone()
            if total <= max_rows:
                return 0
            excess = total - max_rows
            cur = conn.execute(
                "DELETE FROM window_events WHERE id IN ("
                " SELECT id FROM window_events WHERE status = ? "
                " ORDER BY id LIMIT ?"
                ")",
                (STATUS_UPLOADED, int(excess)),
            )
            conn.commit()
            n = cur.rowcount
        if 0 < n < excess:
            logger.warning(
                "buffer enforce: total=%d max=%d，仅删 %d 条 uploaded；"
                "pending+invalid 仍占用导致超限",
                total,
                max_rows,
                n,
            )
        elif n > 0:
            logger.warning(
                "buffer enforce: total=%d > max=%d，已删 %d 条最旧 uploaded",
                total,
                max_rows,
                n,
            )
        elif total > max_rows:
            logger.warning(
                "buffer enforce: total=%d > max=%d 但无 uploaded 可删（全为 pending/invalid）",
                total,
                max_rows,
            )
        return n

    # ---------------------------------------------------------------------
    # 诊断
    # ---------------------------------------------------------------------

    def stats(self) -> dict[str, int]:
        """返回各状态行数：{pending, uploaded, invalid}。监控 / 调试用。"""
        with self._connect() as conn:
            rows = conn.execute(
                "SELECT status, COUNT(*) FROM window_events GROUP BY status"
            ).fetchall()
        out = {STATUS_PENDING: 0, STATUS_UPLOADED: 0, STATUS_INVALID: 0}
        for status, n in rows:
            out[status] = n
        return out

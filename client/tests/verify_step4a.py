"""Step 4A 验证：WindowBuffer 单元契约。

跑法（在 client/ 目录）：
    python tests/verify_step4a.py

覆盖：
- CRUD：append / fetch_pending / mark_uploaded / quarantine / increment_attempt / stats
- 状态机：pending → uploaded ；pending → invalid ；UNIQUE 幂等
- cleanup_uploaded / enforce_max_rows：绝不触及 pending 与 invalid
- WAL 持久化 / crash recovery：子进程写入 + os._exit(1) 模拟崩溃；父进程重开同文件
- 隐私：所有 INFO/WARN 日志不含 process_name / window_title
"""
from __future__ import annotations

import logging
import os
import subprocess
import sys
import tempfile
import textwrap
import time
import uuid
from pathlib import Path

try:
    sys.stdout.reconfigure(encoding="utf-8")  # type: ignore[attr-defined]
    sys.stderr.reconfigure(encoding="utf-8")  # type: ignore[attr-defined]
except Exception:  # noqa: BLE001
    pass

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from app.window_buffer import (  # noqa: E402
    STATUS_INVALID, STATUS_PENDING, STATUS_UPLOADED, WindowBuffer,
)


# ---------------------------------------------------------------------------
# helpers
# ---------------------------------------------------------------------------

def _ok(name: str) -> None:
    print(f"[ok] {name}")


def _fail(name: str, detail: str) -> None:
    print(f"[FAIL] {name}: {detail}")
    sys.exit(1)


def _assert_eq(name: str, got, expected) -> None:
    if got != expected:
        _fail(name, f"expected={expected!r} got={got!r}")
    _ok(name)


def _append(buf: WindowBuffer, *, eid: str | None = None,
            proc: str = "p.exe", title: str = "t",
            duration: int = 1) -> str:
    eid = eid or uuid.uuid4().hex
    buf.append_event(
        client_event_id=eid,
        process_name=proc,
        window_title=title,
        started_at="2026-01-01T09:00:00Z",
        ended_at="2026-01-01T09:00:01Z",
        duration_seconds=duration,
        had_input=True,
    )
    return eid


# ---------------------------------------------------------------------------
# 1. CRUD + UNIQUE 幂等
# ---------------------------------------------------------------------------

def test_crud_and_unique(buf_path: Path) -> None:
    buf = WindowBuffer(buf_path)
    eid = _append(buf, proc="chrome.exe", title="A")
    rows = buf.fetch_pending(10)
    _assert_eq("CRUD: pending=1 after append", len(rows), 1)
    _assert_eq("CRUD: row.client_event_id 一致", rows[0]["client_event_id"], eid)
    _assert_eq("CRUD: row.had_input 解码为 bool", rows[0]["had_input"], True)

    # UNIQUE 幂等：同 client_event_id 再 append → False，不再增行
    ok = buf.append_event(
        client_event_id=eid,
        process_name="chrome.exe", window_title="A",
        started_at="2026-01-01T09:00:00Z",
        ended_at="2026-01-01T09:00:01Z",
        duration_seconds=1, had_input=True,
    )
    _assert_eq("UNIQUE: 重复 append 返回 False", ok, False)
    _assert_eq("UNIQUE: 行数不变", len(buf.fetch_pending(10)), 1)


# ---------------------------------------------------------------------------
# 2. 状态机：pending → uploaded
# ---------------------------------------------------------------------------

def test_mark_uploaded(buf_path: Path) -> None:
    buf = WindowBuffer(buf_path)
    ids = [_append(buf) for _ in range(5)]
    n = buf.mark_uploaded(ids)
    _assert_eq("mark_uploaded: 影响行数=5", n, 5)
    s = buf.stats()
    _assert_eq("mark_uploaded: pending=0", s[STATUS_PENDING], 0)
    _assert_eq("mark_uploaded: uploaded=5", s[STATUS_UPLOADED], 5)

    # 已 uploaded 再 mark → 0 行（守卫 status=pending）
    n2 = buf.mark_uploaded(ids)
    _assert_eq("mark_uploaded: 第二次=0（守卫 pending）", n2, 0)


# ---------------------------------------------------------------------------
# 3. 状态机：pending → invalid（quarantine）+ increment_attempt
# ---------------------------------------------------------------------------

def test_quarantine_and_attempts(buf_path: Path) -> None:
    buf = WindowBuffer(buf_path)
    eid = _append(buf)

    a1 = buf.increment_attempt(eid)
    a2 = buf.increment_attempt(eid)
    a3 = buf.increment_attempt(eid)
    _assert_eq("increment_attempt: 累加到 3", (a1, a2, a3), (1, 2, 3))
    # 状态仍 pending（increment_attempt 不改 status）
    _assert_eq("increment_attempt: 不改 status",
               buf.stats()[STATUS_PENDING], 1)

    ok = buf.quarantine_event(eid, reason="test")
    _assert_eq("quarantine: 成功", ok, True)
    s = buf.stats()
    _assert_eq("quarantine: pending=0", s[STATUS_PENDING], 0)
    _assert_eq("quarantine: invalid=1", s[STATUS_INVALID], 1)

    # quarantine 之后再 mark_uploaded 应失败（守卫 pending）
    n = buf.mark_uploaded([eid])
    _assert_eq("quarantine 后 mark_uploaded=0", n, 0)


# ---------------------------------------------------------------------------
# 4. cleanup_uploaded：只删 uploaded，绝不触及 pending / invalid
# ---------------------------------------------------------------------------

def test_cleanup_uploaded(buf_path: Path) -> None:
    buf = WindowBuffer(buf_path)
    pending_id = _append(buf, proc="pending.exe")
    invalid_id = _append(buf, proc="invalid.exe")
    uploaded_ids = [_append(buf) for _ in range(3)]
    buf.quarantine_event(invalid_id, reason="seed")
    buf.mark_uploaded(uploaded_ids)

    # 直接把 uploaded_at 改成 48h 前，强制超 retention
    import sqlite3
    conn = sqlite3.connect(str(buf_path))
    conn.execute(
        "UPDATE window_events SET uploaded_at = datetime('now', '-48 hours') "
        "WHERE status = ?", (STATUS_UPLOADED,),
    )
    conn.commit()
    conn.close()

    n = buf.cleanup_uploaded(retention_hours=24)
    _assert_eq("cleanup_uploaded: 删除 uploaded=3", n, 3)
    s = buf.stats()
    _assert_eq("cleanup_uploaded: pending 不动",
               s[STATUS_PENDING], 1)
    _assert_eq("cleanup_uploaded: invalid 不动",
               s[STATUS_INVALID], 1)


# ---------------------------------------------------------------------------
# 5. enforce_max_rows：只删最旧的 uploaded
# ---------------------------------------------------------------------------

def test_enforce_max_rows(buf_path: Path) -> None:
    buf = WindowBuffer(buf_path)
    pending_id = _append(buf, proc="pending.exe")
    uploaded_ids = [_append(buf) for _ in range(10)]
    buf.mark_uploaded(uploaded_ids)

    # 行数 = 11；上限 6 → 应删 5 行最旧的 uploaded
    n = buf.enforce_max_rows(max_rows=6)
    _assert_eq("enforce_max_rows: 删 5 条", n, 5)
    s = buf.stats()
    _assert_eq("enforce_max_rows: pending 不动",
               s[STATUS_PENDING], 1)
    _assert_eq("enforce_max_rows: uploaded 保留 5",
               s[STATUS_UPLOADED], 5)


# ---------------------------------------------------------------------------
# 6. WAL 持久化 / crash recovery（子进程 os._exit(1) 模拟崩溃）
# ---------------------------------------------------------------------------

_CHILD_SRC = textwrap.dedent(
    """
    import os, sys
    sys.path.insert(0, r"{root}")
    from app.window_buffer import WindowBuffer
    buf = WindowBuffer(r"{db}")
    for i in range(5):
        buf.append_event(
            client_event_id=f"crash-{{i:02d}}",
            process_name=f"crash{{i}}.exe",
            window_title="t",
            started_at="2026-01-01T09:00:00Z",
            ended_at="2026-01-01T09:00:01Z",
            duration_seconds=1,
            had_input=True,
        )
    # 立即 os._exit：跳过 atexit / WAL checkpoint
    os._exit(1)
    """
).strip()


def test_wal_crash_recovery(buf_path: Path) -> None:
    child = subprocess.run(
        [sys.executable, "-c",
         _CHILD_SRC.format(root=str(Path(__file__).resolve().parents[1]),
                           db=str(buf_path))],
        capture_output=True, timeout=30,
    )
    _assert_eq("WAL: 子进程 exit=1（模拟崩溃）", child.returncode, 1)

    # 父进程打开同一 db
    buf = WindowBuffer(buf_path)
    s = buf.stats()
    _assert_eq("WAL recovery: pending=5（WAL 已持久化）",
               s[STATUS_PENDING], 5)
    rows = buf.fetch_pending(10)
    eids = sorted(r["client_event_id"] for r in rows)
    _assert_eq("WAL recovery: client_event_id 一一对应",
               eids, [f"crash-{i:02d}" for i in range(5)])


# ---------------------------------------------------------------------------
# 7. 隐私：INFO/WARN 不含 process_name / window_title
# ---------------------------------------------------------------------------

def test_no_pii_in_logs(buf_path: Path) -> None:
    buf = WindowBuffer(buf_path)
    SECRET_PROC = "PII-PROC-ABC.exe"
    SECRET_TITLE = "PII-TITLE-XYZ"

    captured: list[logging.LogRecord] = []

    class _Cap(logging.Handler):
        def emit(self, record: logging.LogRecord) -> None:
            captured.append(record)

    lg = logging.getLogger("app.window_buffer")
    cap = _Cap(level=logging.DEBUG)
    lg.addHandler(cap)
    lg.setLevel(logging.DEBUG)
    try:
        # 走 append / mark / quarantine / cleanup / enforce 各路径
        for i in range(20):
            buf.append_event(
                client_event_id=uuid.uuid4().hex,
                process_name=SECRET_PROC,
                window_title=SECRET_TITLE,
                started_at="2026-01-01T09:00:00Z",
                ended_at="2026-01-01T09:00:01Z",
                duration_seconds=1, had_input=True,
            )
        rows = buf.fetch_pending(100)
        buf.mark_uploaded([r["client_event_id"] for r in rows[:10]])
        buf.quarantine_event(rows[10]["client_event_id"], reason="audit")
        # 让 cleanup 真删数据
        import sqlite3
        conn = sqlite3.connect(str(buf_path))
        conn.execute(
            "UPDATE window_events SET uploaded_at=datetime('now','-48 hours') "
            "WHERE status=?", (STATUS_UPLOADED,),
        )
        conn.commit(); conn.close()
        buf.cleanup_uploaded(retention_hours=24)
        buf.enforce_max_rows(max_rows=3)
    finally:
        lg.removeHandler(cap)

    info_warn = [r for r in captured if r.levelno >= logging.INFO]
    forbidden = (SECRET_PROC, SECRET_TITLE, "PII-PROC", "PII-TITLE")
    for r in info_warn:
        msg = r.getMessage()
        for f in forbidden:
            if f in msg:
                _fail("隐私：INFO/WARN", f"日志泄露 {f!r}: {msg!r}")
    _ok(f"隐私：INFO/WARN={len(info_warn)} 条 / DEBUG+={len(captured)} 条，均无 process/title")


# ---------------------------------------------------------------------------
# 入口
# ---------------------------------------------------------------------------

def main() -> int:
    with tempfile.TemporaryDirectory(ignore_cleanup_errors=True) as tmp:
        tmp_path = Path(tmp)
        test_crud_and_unique(tmp_path / "crud.db")
        test_mark_uploaded(tmp_path / "mark.db")
        test_quarantine_and_attempts(tmp_path / "quar.db")
        test_cleanup_uploaded(tmp_path / "cleanup.db")
        test_enforce_max_rows(tmp_path / "enforce.db")
        test_wal_crash_recovery(tmp_path / "wal.db")
        test_no_pii_in_logs(tmp_path / "pii.db")
    print()
    print("ALL Step 4A verifications PASSED")
    return 0


if __name__ == "__main__":
    sys.exit(main())

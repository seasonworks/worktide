"""Step 4C 验证：A–H 场景。

跑法（在 client/ 目录）：
    python tests/verify_step4c.py

所有断言通过则输出 [ok]，任何失败立刻 exit 1。
不依赖网络；所有 HTTP 由 _FakeSession 模拟。
"""
from __future__ import annotations

import logging
import sys
import tempfile
import uuid
from pathlib import Path

try:
    sys.stdout.reconfigure(encoding="utf-8")  # type: ignore[attr-defined]
    sys.stderr.reconfigure(encoding="utf-8")  # type: ignore[attr-defined]
except Exception:  # noqa: BLE001
    pass

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

import requests  # noqa: E402

from app.config import WindowTrackingConfig  # noqa: E402
from app.window_buffer import (  # noqa: E402
    STATUS_INVALID,
    STATUS_PENDING,
    STATUS_UPLOADED,
    WindowBuffer,
)
from app.window_uploader import WindowUploader, _row_to_event  # noqa: E402


# ---------------------------------------------------------------------------
# 测试工具
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


class _FakeResponse:
    def __init__(self, status_code: int, body: dict | None = None) -> None:
        self.status_code = status_code
        self._body = body or {}

    def json(self) -> dict:
        return self._body


class _FakeSession:
    """可编程的 fake requests.Session。

    set_handler(fn) 接收 (url, payload) -> _FakeResponse | Exception。
    Exception 会以原异常类抛出（模拟 requests.Timeout / ConnectionError）。
    """

    def __init__(self) -> None:
        self.calls: list[dict] = []
        self._handler = None  # type: ignore[var-annotated]

    def set_handler(self, fn) -> None:
        self._handler = fn

    def post(self, url, json=None, timeout=None):  # noqa: A002
        self.calls.append({"url": url, "json": json, "timeout": timeout})
        assert self._handler is not None, "FakeSession handler 未设置"
        result = self._handler(url, json)
        if isinstance(result, Exception):
            raise result
        return result


def _seed(buf: WindowBuffer, n: int, *, prefix: str = "evt") -> list[str]:
    """往 buffer 写 n 条 pending；返回它们的 client_event_id 列表。"""
    ids: list[str] = []
    for i in range(n):
        eid = uuid.uuid4().hex
        ids.append(eid)
        buf.append_event(
            client_event_id=eid,
            process_name=f"{prefix}-proc.exe",
            window_title=f"{prefix}-title-{i}",
            started_at=f"2026-01-01T09:{i // 60:02d}:{i % 60:02d}Z",
            ended_at=f"2026-01-01T09:{i // 60:02d}:{(i + 1) % 60:02d}Z",
            duration_seconds=1,
            had_input=True,
        )
    return ids


def _make_uploader(
    buf: WindowBuffer,
    session: _FakeSession,
    *,
    batch_size: int = 100,
    max_attempts: int = 5,
) -> WindowUploader:
    cfg = WindowTrackingConfig(
        upload_batch_size=batch_size,
        upload_interval_seconds=30,
        upload_backoff_seconds=[5, 30, 120, 300],
        upload_max_attempts=max_attempts,
    )
    return WindowUploader(
        cfg, buf,
        machine_id="MACHINE-X",
        url="http://fake/api/v1/windows/report",
        request_timeout_seconds=5,
        session=session,
    )


# ---------------------------------------------------------------------------
# Payload 形态子用例：与服务端 WindowEventIn 严格对齐
# ---------------------------------------------------------------------------

def test_payload_shape() -> None:
    row = {
        "id": 7,
        "client_event_id": "abc",
        "process_name": "chrome.exe",
        "window_title": "Tab",
        "started_at": "2026-01-01T09:00:00Z",
        "ended_at": "2026-01-01T09:00:30Z",
        "duration_seconds": 30,
        "had_input": True,
        "upload_attempts": 0,
    }
    ev = _row_to_event(row)
    expected_keys = {
        "client_event_id", "process_name", "window_title",
        "started_at", "ended_at", "duration_seconds", "had_input",
    }
    _assert_eq("payload: 字段集合", set(ev.keys()), expected_keys)
    if "id" in ev or "upload_attempts" in ev:
        _fail("payload: 本地字段未剔除", repr(ev))
    _ok("payload: 本地字段已剔除")


# ---------------------------------------------------------------------------
# A. 200 → 全部 uploaded
# ---------------------------------------------------------------------------

def test_A_all_200(buf_path: Path) -> None:
    buf = WindowBuffer(buf_path)
    ids = _seed(buf, 50)
    session = _FakeSession()
    session.set_handler(
        lambda url, payload: _FakeResponse(200, {"accepted": len(payload["events"]), "deduped": 0})
    )
    up = _make_uploader(buf, session, batch_size=100)

    stats = up.drain_once()
    _assert_eq("A: fetched", stats["fetched"], 50)
    _assert_eq("A: uploaded", stats["uploaded"], 50)
    _assert_eq("A: sub_batches", stats["sub_batches"], 1)
    _assert_eq("A: network_error", stats["network_error"], False)

    s = buf.stats()
    _assert_eq("A: buffer pending=0", s[STATUS_PENDING], 0)
    _assert_eq("A: buffer uploaded=50", s[STATUS_UPLOADED], 50)
    _assert_eq("A: buffer invalid=0", s[STATUS_INVALID], 0)

    # 校验 payload：machine_id + 50 events + 服务端 WindowEventIn 字段
    sent = session.calls[0]["json"]
    _assert_eq("A: machine_id 透传", sent["machine_id"], "MACHINE-X")
    _assert_eq("A: events 数", len(sent["events"]), 50)
    # 客户端 ids 与上传 ids 一一对应
    sent_ids = [e["client_event_id"] for e in sent["events"]]
    _assert_eq("A: client_event_id 原样上传", set(sent_ids), set(ids))


# ---------------------------------------------------------------------------
# B. 网络断开 → pending 保留
# ---------------------------------------------------------------------------

def test_B_network_down(buf_path: Path) -> None:
    buf = WindowBuffer(buf_path)
    _seed(buf, 20)
    session = _FakeSession()
    session.set_handler(
        lambda url, payload: requests.ConnectionError("connection refused")
    )
    up = _make_uploader(buf, session)

    stats = up.drain_once()
    _assert_eq("B: network_error 触发", stats["network_error"], True)
    _assert_eq("B: uploaded=0", stats["uploaded"], 0)
    _assert_eq("B: quarantined=0", stats["quarantined"], 0)

    s = buf.stats()
    _assert_eq("B: buffer pending 保留", s[STATUS_PENDING], 20)
    _assert_eq("B: buffer uploaded 仍 0", s[STATUS_UPLOADED], 0)
    _assert_eq("B: buffer invalid 仍 0", s[STATUS_INVALID], 0)


# ---------------------------------------------------------------------------
# C. 5xx → retry 正常（不 mark / 不增 attempts / 走 backoff）
# ---------------------------------------------------------------------------

def test_C_server_5xx(buf_path: Path) -> None:
    buf = WindowBuffer(buf_path)
    ids = _seed(buf, 10)
    session = _FakeSession()
    # 第一次 500，第二次 503，第三次 200
    seq = iter([
        _FakeResponse(500),
        _FakeResponse(503),
        _FakeResponse(200, {"accepted": 10, "deduped": 0}),
    ])
    session.set_handler(lambda url, payload: next(seq))
    up = _make_uploader(buf, session)

    s1 = up.drain_once()
    _assert_eq("C: 第 1 次 5xx → network_error", s1["network_error"], True)
    _assert_eq("C: 第 1 次 reason 含 HTTP", "HTTP" in s1["reason"], True)
    _assert_eq("C: 5xx 后 buffer pending 不变", buf.stats()[STATUS_PENDING], 10)
    # 关键：upload_attempts 未递增
    for r in buf.fetch_pending(100):
        if r["upload_attempts"] != 0:
            _fail("C: 5xx 不应增 attempts", repr(r))
    _ok("C: 5xx 后 upload_attempts 仍为 0（不污染攻击预算）")

    s2 = up.drain_once()
    _assert_eq("C: 第 2 次 5xx", s2["network_error"], True)

    s3 = up.drain_once()
    _assert_eq("C: 第 3 次 200", s3["network_error"], False)
    _assert_eq("C: 最终 uploaded=10", s3["uploaded"], 10)
    _assert_eq("C: 最终 pending=0", buf.stats()[STATUS_PENDING], 0)


# ---------------------------------------------------------------------------
# D. 422 单条 → attempts++
# ---------------------------------------------------------------------------

def test_D_422_increments(buf_path: Path) -> None:
    buf = WindowBuffer(buf_path)
    ids = _seed(buf, 1)
    session = _FakeSession()
    session.set_handler(lambda url, payload: _FakeResponse(422, {"detail": "bad"}))
    up = _make_uploader(buf, session, max_attempts=5)

    stats = up.drain_once()
    _assert_eq("D: fetched", stats["fetched"], 1)
    _assert_eq("D: attempts_added", stats["attempts_added"], 1)
    _assert_eq("D: quarantined", stats["quarantined"], 0)
    _assert_eq("D: uploaded", stats["uploaded"], 0)

    rows = buf.fetch_pending(10)
    _assert_eq("D: pending 仍 1（未 quarantine）", len(rows), 1)
    _assert_eq("D: upload_attempts=1", rows[0]["upload_attempts"], 1)


# ---------------------------------------------------------------------------
# E. attempts >= max → invalid (quarantine)
# ---------------------------------------------------------------------------

def test_E_quarantine_at_threshold(buf_path: Path) -> None:
    buf = WindowBuffer(buf_path)
    ids = _seed(buf, 1)
    session = _FakeSession()
    session.set_handler(lambda url, payload: _FakeResponse(422))
    up = _make_uploader(buf, session, max_attempts=5)

    # 连续 5 次 drain
    for i in range(5):
        up.drain_once()

    s = buf.stats()
    _assert_eq("E: 5 次 422 后 pending=0", s[STATUS_PENDING], 0)
    _assert_eq("E: 5 次 422 后 invalid=1", s[STATUS_INVALID], 1)
    _assert_eq("E: 5 次 422 后 uploaded=0", s[STATUS_UPLOADED], 0)


# ---------------------------------------------------------------------------
# E2. 422 多条 → 二分隔离（关键要求 2：最小粒度隔离）
# ---------------------------------------------------------------------------

def test_E2_bisect_isolates(buf_path: Path) -> None:
    """500 events 中 5 个坏：好的应当被 200 mark；坏的应当独立 attempts。"""
    buf = WindowBuffer(buf_path)
    ids = _seed(buf, 500)
    bad_ids = set(ids[100:105])  # 任意 5 个坏

    def handler(url, payload):
        evs = payload["events"]
        # 模拟服务端：批内任一坏 id 就 422 整批；否则 200
        if any(e["client_event_id"] in bad_ids for e in evs):
            return _FakeResponse(422, {"detail": "bad"})
        return _FakeResponse(200, {"accepted": len(evs), "deduped": 0})

    session = _FakeSession()
    session.set_handler(handler)
    up = _make_uploader(buf, session, batch_size=500, max_attempts=5)

    stats = up.drain_once()
    _assert_eq("E2: fetched=500", stats["fetched"], 500)
    # 495 个好的全部 200 mark
    _assert_eq("E2: uploaded=495（好数据未被冤死）", stats["uploaded"], 495)
    # 5 个坏的每个 attempts +1（5 次 attempts_added，本轮还未 quarantine）
    _assert_eq("E2: attempts_added=5", stats["attempts_added"], 5)
    _assert_eq("E2: quarantined=0（本轮 attempts=1，未触达）", stats["quarantined"], 0)
    # POST 次数 ≤ log2(500) * 5 + 1 ≈ 50；正确性更重要，这里只要 < 500
    if stats["sub_batches"] >= 500:
        _fail("E2: sub_batches 应远小于 500", str(stats["sub_batches"]))
    _ok(f"E2: sub_batches={stats['sub_batches']}（远小于 N=500，证明二分有效）")

    s = buf.stats()
    _assert_eq("E2: 1 轮 drain 后 uploaded=495", s[STATUS_UPLOADED], 495)
    _assert_eq("E2: 1 轮 drain 后 pending=5（5 个坏的留待下一轮）", s[STATUS_PENDING], 5)
    _assert_eq("E2: 1 轮 drain 后 invalid=0", s[STATUS_INVALID], 0)

    # 再 4 轮 drain → 5 个坏的 attempts 累积到 5 → quarantine
    for _ in range(4):
        up.drain_once()
    s = buf.stats()
    _assert_eq("E2: 5 轮 drain 后 pending=0", s[STATUS_PENDING], 0)
    _assert_eq("E2: 5 轮 drain 后 invalid=5", s[STATUS_INVALID], 5)
    _assert_eq("E2: 5 轮 drain 后 uploaded=495", s[STATUS_UPLOADED], 495)


# ---------------------------------------------------------------------------
# F. 重启 uploader → pending 自动续传
# ---------------------------------------------------------------------------

def test_F_restart_resumes(buf_path: Path) -> None:
    buf = WindowBuffer(buf_path)
    _seed(buf, 30)

    # 第一阶段：网络断
    session1 = _FakeSession()
    session1.set_handler(lambda url, payload: requests.ConnectionError("dead"))
    up1 = _make_uploader(buf, session1)
    up1.drain_once()
    _assert_eq("F: 阶段 1 pending 保留", buf.stats()[STATUS_PENDING], 30)

    # 第二阶段：新建 uploader（模拟进程重启），网络恢复
    session2 = _FakeSession()
    session2.set_handler(
        lambda url, payload: _FakeResponse(200, {"accepted": 30, "deduped": 0})
    )
    up2 = _make_uploader(buf, session2)
    stats = up2.drain_once()
    _assert_eq("F: 阶段 2 uploaded=30（无需 tracker 重新产）", stats["uploaded"], 30)
    _assert_eq("F: 阶段 2 pending=0", buf.stats()[STATUS_PENDING], 0)


# ---------------------------------------------------------------------------
# G. client_event_id 重复 → 服务端 dedupe，客户端正常处理
# ---------------------------------------------------------------------------

def test_G_deduped(buf_path: Path) -> None:
    buf = WindowBuffer(buf_path)
    _seed(buf, 20)

    session = _FakeSession()
    # 假设全部已在服务端：accepted=0, deduped=20，仍 200
    session.set_handler(
        lambda url, payload: _FakeResponse(200, {"accepted": 0, "deduped": 20})
    )
    up = _make_uploader(buf, session)
    stats = up.drain_once()

    _assert_eq("G: 200 含 deduped 仍 uploaded", stats["uploaded"], 20)
    _assert_eq("G: deduped 透传到 stats", stats["deduped"], 20)
    _assert_eq("G: pending=0", buf.stats()[STATUS_PENDING], 0)
    _assert_eq("G: uploaded=20", buf.stats()[STATUS_UPLOADED], 20)


# ---------------------------------------------------------------------------
# H. 10000 pending → 分批 drain → 最终清空
# ---------------------------------------------------------------------------

def test_H_large_backlog(buf_path: Path) -> None:
    buf = WindowBuffer(buf_path)
    N = 10_000
    BATCH = 500
    _seed(buf, N)
    session = _FakeSession()
    session.set_handler(
        lambda url, payload: _FakeResponse(200, {"accepted": len(payload["events"]), "deduped": 0})
    )
    up = _make_uploader(buf, session, batch_size=BATCH)

    drains = 0
    while True:
        stats = up.drain_once()
        drains += 1
        if stats["fetched"] == 0:
            break
        if drains > N // BATCH + 5:
            _fail("H: drain 次数失控", str(drains))

    _assert_eq("H: 最终 pending=0", buf.stats()[STATUS_PENDING], 0)
    _assert_eq("H: 最终 uploaded=N", buf.stats()[STATUS_UPLOADED], N)
    # 期望 drain 次数 ~ N / BATCH + 1（最后一拍 fetched=0）
    _assert_eq(
        f"H: drain 次数 = N/BATCH+1 = {N // BATCH + 1}", drains, N // BATCH + 1
    )


# ---------------------------------------------------------------------------
# I. 隐私：INFO/WARN 不出现 process_name / window_title
# ---------------------------------------------------------------------------

def test_I_no_pii_in_logs(buf_path: Path) -> None:
    buf = WindowBuffer(buf_path)
    # 用显眼标识便于扫描
    SECRET_PROC = "SECRET-PROCESS-Z.exe"
    SECRET_TITLE = "SECRET-TITLE-W"

    for i in range(3):
        buf.append_event(
            client_event_id=uuid.uuid4().hex,
            process_name=SECRET_PROC,
            window_title=SECRET_TITLE,
            started_at="2026-01-01T09:00:00Z",
            ended_at="2026-01-01T09:00:01Z",
            duration_seconds=1,
            had_input=True,
        )

    captured: list[logging.LogRecord] = []

    class _Cap(logging.Handler):
        def emit(self, record: logging.LogRecord) -> None:
            captured.append(record)

    cap = _Cap(level=logging.DEBUG)
    for name in ("app.window_uploader", "app.window_buffer"):
        lg = logging.getLogger(name)
        lg.addHandler(cap)
        lg.setLevel(logging.DEBUG)

    try:
        # 1) 走 200 路径
        session = _FakeSession()
        session.set_handler(
            lambda url, payload: _FakeResponse(200, {"accepted": 3, "deduped": 0})
        )
        up = _make_uploader(buf, session)
        up.drain_once()

        # 2) 走 422 → quarantine 路径（再 seed 一条）
        eid = uuid.uuid4().hex
        buf.append_event(
            client_event_id=eid,
            process_name=SECRET_PROC,
            window_title=SECRET_TITLE,
            started_at="2026-01-01T09:01:00Z",
            ended_at="2026-01-01T09:01:01Z",
            duration_seconds=1,
            had_input=True,
        )
        session2 = _FakeSession()
        session2.set_handler(lambda url, payload: _FakeResponse(422))
        up2 = _make_uploader(buf, session2, max_attempts=2)
        up2.drain_once()
        up2.drain_once()  # 触发 quarantine
    finally:
        for name in ("app.window_uploader", "app.window_buffer"):
            logging.getLogger(name).removeHandler(cap)

    info_warn = [r for r in captured if r.levelno >= logging.INFO]
    forbidden = (SECRET_PROC, SECRET_TITLE, "SECRET-PROCESS", "SECRET-TITLE")
    for r in info_warn:
        msg = r.getMessage()
        for f in forbidden:
            if f in msg:
                _fail("I: INFO/WARN 隐私", f"日志泄露 {f!r}: {msg!r}")
    _ok(
        f"I: 捕获 {len(captured)} 条（INFO/WARN={len(info_warn)} 条），"
        f"均无 process/title"
    )


# ---------------------------------------------------------------------------
# 入口
# ---------------------------------------------------------------------------

def main() -> int:
    with tempfile.TemporaryDirectory(ignore_cleanup_errors=True) as tmp:
        tmp_path = Path(tmp)
        test_payload_shape()
        test_A_all_200(tmp_path / "A.db")
        test_B_network_down(tmp_path / "B.db")
        test_C_server_5xx(tmp_path / "C.db")
        test_D_422_increments(tmp_path / "D.db")
        test_E_quarantine_at_threshold(tmp_path / "E.db")
        test_E2_bisect_isolates(tmp_path / "E2.db")
        test_F_restart_resumes(tmp_path / "F.db")
        test_G_deduped(tmp_path / "G.db")
        test_H_large_backlog(tmp_path / "H.db")
        test_I_no_pii_in_logs(tmp_path / "I.db")
    print()
    print("ALL Step 4C verifications PASSED")
    return 0


if __name__ == "__main__":
    sys.exit(main())

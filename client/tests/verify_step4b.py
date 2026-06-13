"""Step 4B 验证：A–G 场景。

跑法（在 client/ 目录下）：
    python tests/verify_step4b.py

所有断言通过则输出 [ok]，任何失败立刻 exit 1。
故意不使用 unittest 框架以便人工可读 + 与 Step 4A verify 脚本风格一致。
"""
from __future__ import annotations

import logging
import sys
import tempfile
from datetime import datetime, timedelta, timezone
from pathlib import Path

# Windows 控制台默认 cp936，中文断言名会乱码；强制 utf-8 stdout/stderr
try:
    sys.stdout.reconfigure(encoding="utf-8")  # type: ignore[attr-defined]
    sys.stderr.reconfigure(encoding="utf-8")  # type: ignore[attr-defined]
except Exception:  # noqa: BLE001
    pass

# 让 `python tests/verify_step4b.py` 在 client/ 目录下也能找到 app.*
sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from app.config import WindowTrackingConfig  # noqa: E402
from app.window_buffer import STATUS_PENDING, WindowBuffer  # noqa: E402
from app.window_tracker import (  # noqa: E402
    WindowTracker,
    normalize_window_title,
)


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


def _make_tracker(
    cfg: WindowTrackingConfig,
    buffer: WindowBuffer,
    *,
    sampler,
    idle_fn,
    clock,
) -> WindowTracker:
    return WindowTracker(
        cfg,
        buffer,
        sampler=sampler,
        idle_provider=idle_fn,
        clock=clock,
    )


class _Clock:
    """手动推进的时钟。tracker 调用 clock() 时返回 .now。"""

    def __init__(self, start: datetime) -> None:
        self.now = start

    def __call__(self) -> datetime:
        return self.now

    def advance(self, seconds: int) -> None:
        self.now = self.now + timedelta(seconds=seconds)


class _Sampler:
    """可手动设置 (process, title) 或 None 的采样器。"""

    def __init__(self) -> None:
        self.value = None  # type: ignore[var-annotated]

    def __call__(self):
        return self.value


class _Idle:
    """可手动设置当前 idle 秒数的 idle provider。"""

    def __init__(self) -> None:
        self.value = 0.0

    def __call__(self) -> float:
        return self.value


def _stats(buffer: WindowBuffer) -> dict:
    return buffer.stats()


def _all_events(buffer: WindowBuffer) -> list[dict]:
    return buffer.fetch_pending(limit=10_000)


# ---------------------------------------------------------------------------
# normalize_window_title 子用例（重点要求 3）
# ---------------------------------------------------------------------------

def test_normalize() -> None:
    cases = [
        (None, ""),
        ("", ""),
        ("  hello   world  ", "hello world"),
        ("YouTube (1) - Chrome", "YouTube - Chrome"),
        ("Inbox (3) (Outlook)", "Inbox (Outlook)"),  # 只删纯数字 (N)
        ("(beta) App", "(beta) App"),  # 非纯数字不删
        ("a b　c", "a b c"),  # NBSP / 全角空格归一
        ("(1) Tab (2) Browser", "Tab Browser"),
    ]
    for raw, expected in cases:
        got = normalize_window_title(raw, max_length=200)
        if got != expected:
            _fail(f"normalize({raw!r})", f"expected={expected!r} got={got!r}")
    _ok(f"normalize_window_title {len(cases)}/{len(cases)} cases")

    # length cap
    truncated = normalize_window_title("x" * 500, max_length=120)
    if len(truncated) != 120:
        _fail("normalize length cap", f"len={len(truncated)}")
    _ok("normalize length cap")


# ---------------------------------------------------------------------------
# A. 同 process/title 连续 10 分钟仅产生 1 event
# ---------------------------------------------------------------------------

def test_A_same_window_10min(buf_path: Path) -> None:
    buf = WindowBuffer(buf_path)
    cfg = WindowTrackingConfig(
        sample_interval_seconds=2,
        idle_threshold_seconds=60,
        # 长到 10 分钟不会触发切片
        flush_interval_seconds=3600,
        title_max_length=200,
    )
    clock = _Clock(datetime(2026, 1, 1, 9, 0, 0, tzinfo=timezone.utc))
    sampler = _Sampler()
    sampler.value = ("chrome.exe", "Docs - Chrome")
    idle = _Idle()

    tracker = _make_tracker(
        cfg, buf, sampler=sampler, idle_fn=idle, clock=clock
    )

    # 10 分钟 = 600 秒，2 秒一拍 = 300 拍
    for _ in range(300):
        tracker.tick()
        clock.advance(2)

    # 只 tick 不 stop()：buffer 里应该 0 条（session 还在内存里）
    _assert_eq("A: pending count before stop", _stats(buf)[STATUS_PENDING], 0)

    # stop() 后应该恰好 1 条
    tracker.stop()
    pending = _stats(buf)[STATUS_PENDING]
    _assert_eq("A: pending after stop (10min same window -> 1 event)", pending, 1)

    rows = _all_events(buf)
    if rows[0]["duration_seconds"] not in (598, 600):
        _fail("A: duration", f"got={rows[0]['duration_seconds']}")
    _ok("A: duration ~ 600s")


# ---------------------------------------------------------------------------
# B. process change 正确切 session
# ---------------------------------------------------------------------------

def test_B_process_change(buf_path: Path) -> None:
    buf = WindowBuffer(buf_path)
    cfg = WindowTrackingConfig(
        sample_interval_seconds=2, idle_threshold_seconds=60,
        flush_interval_seconds=3600, title_max_length=200,
    )
    clock = _Clock(datetime(2026, 1, 1, 9, 0, 0, tzinfo=timezone.utc))
    sampler = _Sampler()
    idle = _Idle()
    tracker = _make_tracker(cfg, buf, sampler=sampler, idle_fn=idle, clock=clock)

    # 60s on chrome
    sampler.value = ("chrome.exe", "A - Chrome")
    for _ in range(30):  # 30 * 2s
        tracker.tick(); clock.advance(2)
    # 切到 code.exe
    sampler.value = ("code.exe", "main.py - VSCode")
    for _ in range(30):
        tracker.tick(); clock.advance(2)

    tracker.stop()
    rows = sorted(_all_events(buf), key=lambda r: r["started_at"])
    _assert_eq("B · event 数", len(rows), 2)
    _assert_eq("B · 第一段 process", rows[0]["process_name"], "chrome.exe")
    _assert_eq("B · 第二段 process", rows[1]["process_name"], "code.exe")


# ---------------------------------------------------------------------------
# C. title change 正确切 session
# ---------------------------------------------------------------------------

def test_C_title_change(buf_path: Path) -> None:
    buf = WindowBuffer(buf_path)
    cfg = WindowTrackingConfig(
        sample_interval_seconds=2, idle_threshold_seconds=60,
        flush_interval_seconds=3600, title_max_length=200,
    )
    clock = _Clock(datetime(2026, 1, 1, 9, 0, 0, tzinfo=timezone.utc))
    sampler = _Sampler()
    idle = _Idle()
    tracker = _make_tracker(cfg, buf, sampler=sampler, idle_fn=idle, clock=clock)

    sampler.value = ("chrome.exe", "Tab A - Chrome")
    for _ in range(20):
        tracker.tick(); clock.advance(2)
    # 同 process，换 title
    sampler.value = ("chrome.exe", "Tab B - Chrome")
    for _ in range(20):
        tracker.tick(); clock.advance(2)

    tracker.stop()
    rows = sorted(_all_events(buf), key=lambda r: r["started_at"])
    _assert_eq("C · event 数", len(rows), 2)
    _assert_eq("C · 第一段 title", rows[0]["window_title"], "Tab A - Chrome")
    _assert_eq("C · 第二段 title", rows[1]["window_title"], "Tab B - Chrome")


def test_C2_title_change_only_normalized(buf_path: Path) -> None:
    """同窗口仅未读计数从 (1) → (2) 变化，归一化后相同，不应切 session。"""
    buf = WindowBuffer(buf_path)
    cfg = WindowTrackingConfig(
        sample_interval_seconds=2, idle_threshold_seconds=60,
        flush_interval_seconds=3600, title_max_length=200,
    )
    clock = _Clock(datetime(2026, 1, 1, 9, 0, 0, tzinfo=timezone.utc))
    sampler = _Sampler()
    idle = _Idle()
    tracker = _make_tracker(cfg, buf, sampler=sampler, idle_fn=idle, clock=clock)

    sampler.value = ("chrome.exe", "Inbox (1) - Gmail")
    for _ in range(5):
        tracker.tick(); clock.advance(2)
    sampler.value = ("chrome.exe", "Inbox (2) - Gmail")
    for _ in range(5):
        tracker.tick(); clock.advance(2)
    sampler.value = ("chrome.exe", "Inbox (3) - Gmail")
    for _ in range(5):
        tracker.tick(); clock.advance(2)

    tracker.stop()
    rows = _all_events(buf)
    _assert_eq("C2 · (N) 噪声不应切 session（仍 1 event）", len(rows), 1)
    _assert_eq(
        "C2 · normalized title",
        rows[0]["window_title"], "Inbox - Gmail",
    )


# ---------------------------------------------------------------------------
# D. idle close 正确关闭 session（不开新）
# ---------------------------------------------------------------------------

def test_D_idle_closes(buf_path: Path) -> None:
    buf = WindowBuffer(buf_path)
    cfg = WindowTrackingConfig(
        sample_interval_seconds=2, idle_threshold_seconds=60,
        flush_interval_seconds=3600, title_max_length=200,
    )
    clock = _Clock(datetime(2026, 1, 1, 9, 0, 0, tzinfo=timezone.utc))
    sampler = _Sampler()
    idle = _Idle()
    tracker = _make_tracker(cfg, buf, sampler=sampler, idle_fn=idle, clock=clock)

    # 工作 5 分钟
    sampler.value = ("chrome.exe", "Work - Chrome")
    for _ in range(150):  # 150 * 2s = 300s
        tracker.tick(); clock.advance(2)
    # 一上来就 idle，立刻 flush（不再算入 session）
    idle.value = 120
    tracker.tick(); clock.advance(2)

    # 此时已 flush 完，buffer 应有 1 条；session 应被清空（不开新）
    _assert_eq("D · idle 时 pending 数", _stats(buf)[STATUS_PENDING], 1)
    if tracker._session is not None:  # type: ignore[attr-defined]  内部断言
        _fail("D · idle 后内存 session", "应为 None")
    _ok("D · idle 后内存 session 已清空")

    # 用户回来 15 分钟后再操作：开新 session
    idle.value = 0
    for _ in range(60):  # 60 * 2s = 120s
        tracker.tick(); clock.advance(2)

    tracker.stop()
    rows = sorted(_all_events(buf), key=lambda r: r["started_at"])
    _assert_eq("D · 回来后总 event 数", len(rows), 2)
    # 第一段 duration 应该 ≈ 298–300s（不算 idle 时间）
    if not (290 <= rows[0]["duration_seconds"] <= 302):
        _fail("D · 第一段 duration", f"got={rows[0]['duration_seconds']}")
    _ok(f"D · 第一段 duration={rows[0]['duration_seconds']}s（≈300，不含 idle）")


# ---------------------------------------------------------------------------
# E. tracker stop() flush open session
# ---------------------------------------------------------------------------

def test_E_stop_flushes(buf_path: Path) -> None:
    buf = WindowBuffer(buf_path)
    cfg = WindowTrackingConfig(
        sample_interval_seconds=2, idle_threshold_seconds=60,
        flush_interval_seconds=3600, title_max_length=200,
    )
    clock = _Clock(datetime(2026, 1, 1, 9, 0, 0, tzinfo=timezone.utc))
    sampler = _Sampler()
    idle = _Idle()
    tracker = _make_tracker(cfg, buf, sampler=sampler, idle_fn=idle, clock=clock)

    sampler.value = ("notepad.exe", "Untitled - Notepad")
    for _ in range(10):
        tracker.tick(); clock.advance(2)

    # 停止前一定有内存 session
    if tracker._session is None:  # type: ignore[attr-defined]
        _fail("E · 停止前", "session 不应为 None")
    _assert_eq("E · 停止前 pending 数", _stats(buf)[STATUS_PENDING], 0)

    tracker.stop()
    _assert_eq("E · stop 后 pending 数", _stats(buf)[STATUS_PENDING], 1)


# ---------------------------------------------------------------------------
# F. window_title 不进入 INFO / WARNING 日志（隐私红线）
# ---------------------------------------------------------------------------

def test_F_no_title_in_logs(buf_path: Path) -> None:
    buf = WindowBuffer(buf_path)
    cfg = WindowTrackingConfig(
        sample_interval_seconds=2, idle_threshold_seconds=60,
        flush_interval_seconds=4, title_max_length=200,
    )
    clock = _Clock(datetime(2026, 1, 1, 9, 0, 0, tzinfo=timezone.utc))
    sampler = _Sampler()
    idle = _Idle()
    tracker = _make_tracker(cfg, buf, sampler=sampler, idle_fn=idle, clock=clock)

    secret_proc = "SECRET-PROCESS-X.exe"
    secret_title = "SECRET-TITLE-YYYY"
    sampler.value = (secret_proc, secret_title)

    # 捕获 app.window_tracker 的全部日志（DEBUG 及以上）
    records: list[logging.LogRecord] = []

    class _Cap(logging.Handler):
        def emit(self, record: logging.LogRecord) -> None:
            records.append(record)

    cap = _Cap(level=logging.DEBUG)
    tracker_logger = logging.getLogger("app.window_tracker")
    buffer_logger = logging.getLogger("app.window_buffer")
    tracker_logger.addHandler(cap)
    buffer_logger.addHandler(cap)
    tracker_logger.setLevel(logging.DEBUG)
    buffer_logger.setLevel(logging.DEBUG)

    try:
        tracker.start()  # 这里会发 INFO「window tracker 启动 ...」
        # 用 tick 而不是真线程，避免抖动
        # 但 start() 已经启动线程；为简化，立刻 stop（线程刚跑就退出）。
        # 再手动 tick 几次 + 切窗口触发 flush。
        tracker.stop()

        tracker2 = _make_tracker(
            cfg, buf, sampler=sampler, idle_fn=idle, clock=clock
        )
        for _ in range(5):
            tracker2.tick(); clock.advance(2)
        sampler.value = (secret_proc, secret_title + " v2")
        for _ in range(5):
            tracker2.tick(); clock.advance(2)
        tracker2.stop()
    finally:
        tracker_logger.removeHandler(cap)
        buffer_logger.removeHandler(cap)

    info_warn = [
        r for r in records if r.levelno >= logging.INFO
    ]
    forbidden = (secret_proc, secret_title, "SECRET-TITLE", "SECRET-PROCESS")
    for r in info_warn:
        msg = r.getMessage()
        for f in forbidden:
            if f in msg:
                _fail(
                    "F · INFO/WARN 隐私",
                    f"日志泄露 {f!r}: {msg!r}",
                )
    _ok(f"F · 捕获 {len(records)} 条（INFO/WARN={len(info_warn)} 条），均无 process/title")


# ---------------------------------------------------------------------------
# G. append_event 真实写入 WindowBuffer
# ---------------------------------------------------------------------------

def test_G_real_buffer_write(buf_path: Path) -> None:
    buf = WindowBuffer(buf_path)
    cfg = WindowTrackingConfig(
        sample_interval_seconds=2, idle_threshold_seconds=60,
        flush_interval_seconds=3600, title_max_length=200,
    )
    clock = _Clock(datetime(2026, 1, 1, 9, 0, 0, tzinfo=timezone.utc))
    sampler = _Sampler()
    idle = _Idle()
    tracker = _make_tracker(cfg, buf, sampler=sampler, idle_fn=idle, clock=clock)

    sampler.value = ("zoom.exe", "Meeting - Zoom")
    for _ in range(3):
        tracker.tick(); clock.advance(2)
    sampler.value = ("teams.exe", "Chat - Teams")
    for _ in range(3):
        tracker.tick(); clock.advance(2)
    tracker.stop()

    rows = sorted(_all_events(buf), key=lambda r: r["started_at"])
    _assert_eq("G · event 数", len(rows), 2)

    z = rows[0]
    if not (z["client_event_id"] and len(z["client_event_id"]) == 32):
        _fail("G · client_event_id 格式", repr(z["client_event_id"]))
    _ok("G · client_event_id 为 32 hex")

    expected_keys = {
        "process_name", "window_title", "started_at", "ended_at",
        "duration_seconds", "had_input", "upload_attempts",
    }
    missing = expected_keys - set(z.keys())
    if missing:
        _fail("G · 字段完整性", f"缺少 {missing}")
    _ok("G · 字段完整性 (id/proc/title/started/ended/duration/had_input/attempts)")

    if not z["started_at"].endswith("Z"):
        _fail("G · started_at 后缀", z["started_at"])
    if not z["ended_at"].endswith("Z"):
        _fail("G · ended_at 后缀", z["ended_at"])
    _ok("G · ISO 8601 UTC ('Z') 后缀")

    if z["had_input"] is not True:
        _fail("G · had_input", repr(z["had_input"]))
    _ok("G · had_input=True")


# ---------------------------------------------------------------------------
# 入口
# ---------------------------------------------------------------------------

def main() -> int:
    # ignore_cleanup_errors: Windows SQLite WAL 句柄释放节奏与 rmtree 冲突，
    # 让 Python 自己吞掉 PermissionError；OS 重启会清理 tmp 目录。
    with tempfile.TemporaryDirectory(ignore_cleanup_errors=True) as tmp:
        tmp_path = Path(tmp)
        test_normalize()
        test_A_same_window_10min(tmp_path / "A.db")
        test_B_process_change(tmp_path / "B.db")
        test_C_title_change(tmp_path / "C.db")
        test_C2_title_change_only_normalized(tmp_path / "C2.db")
        test_D_idle_closes(tmp_path / "D.db")
        test_E_stop_flushes(tmp_path / "E.db")
        test_F_no_title_in_logs(tmp_path / "F.db")
        test_G_real_buffer_write(tmp_path / "G.db")
    print()
    print("ALL Step 4B verifications PASSED")
    return 0


if __name__ == "__main__":
    sys.exit(main())

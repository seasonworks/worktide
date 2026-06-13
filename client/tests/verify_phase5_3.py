"""Phase 5.3 · Health Reporter + version 单元验证。

跑法（在 client/）::

    python tests/verify_phase5_3.py

与 verify_phase5_1.py / verify_phase5_2.py 同风格：手写 case() 入口，
无 pytest 依赖。

覆盖：
  Version --------------------------------------------------------------
    V1  AGENT_VERSION 非空字符串

  HealthConfig ---------------------------------------------------------
    C1  默认值符合设计 D1（120s）+ D3（dataclass 默认即可工作）

  HealthReporter.build_payload ---------------------------------------
    R1  字段齐全（与 server schemas.HealthReportIn 对应）
    R2  pending_events 取自 window_buffer.stats()['pending']
    R3  window_buffer=None → pending_events = 0
    R4  watchdog=None → payload['watchdog'] is None
    R5  watchdog 注入 → payload['watchdog'] 含 ages/misses/thresholds/enabled
    R6  lifecycle 文件不存在 → first/last_start_at 为 None，不抛错
    R7  lifecycle 中 last_exit_reason=watchdog_tracker_timeout → 透传
    R8  uptime_seconds 单调递增

  HealthReporter._report_once（注入 fake requests.Session）----------
    N1  resp 200 → True
    N2  resp 500 → False（让外层退避重试）
    N3  resp 422 → False（schema 错误，ERROR log 不阻塞下个周期）
    N4  requests.RequestException → False（网络错误，WARN log）
"""
from __future__ import annotations

import json
import sys
import tempfile
from datetime import datetime, timezone
from pathlib import Path

try:
    sys.stdout.reconfigure(encoding="utf-8")  # type: ignore[attr-defined]
    sys.stderr.reconfigure(encoding="utf-8")  # type: ignore[attr-defined]
except Exception:  # noqa: BLE001
    pass

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

import requests  # noqa: E402  for fake session signature

from app.config import HealthConfig  # noqa: E402
from app.health_reporter import HealthReporter  # noqa: E402
from app.lifecycle import LifecycleRecorder  # noqa: E402
from app.version import AGENT_VERSION  # noqa: E402

_pass = 0
_fail = 0


def case(name: str, fn) -> None:
    global _pass, _fail
    try:
        fn()
        print(f"[ok] {name}")
        _pass += 1
    except Exception as e:  # noqa: BLE001
        print(f"[FAIL] {name}: {e}")
        _fail += 1


# ---------------------------------------------------------------------------
# Fakes
# ---------------------------------------------------------------------------


class _FakeBuffer:
    """模拟 WindowBuffer.stats() 返回。"""

    def __init__(self, pending: int) -> None:
        self._pending = pending

    def stats(self) -> dict[str, int]:
        return {"pending": self._pending, "uploaded": 0, "invalid": 0}


class _FakeWatchdog:
    """模拟 Watchdog.snapshot_for_reporting()。"""

    def __init__(self, payload: dict) -> None:
        self._payload = payload

    def snapshot_for_reporting(self) -> dict:
        return dict(self._payload)


class _FakeResponse:
    def __init__(self, status_code: int, text: str = "") -> None:
        self.status_code = status_code
        self.text = text


class _FakeSession:
    """模拟 requests.Session.post。

    behavior 可以是：
      int (status code)
      Exception 实例（被 raise）
    """

    def __init__(self, behavior) -> None:  # noqa: ANN001
        self._behavior = behavior
        self.calls: list[tuple[str, dict, int]] = []

    def post(self, url: str, *, json=None, timeout: int = 0):  # noqa: ANN001
        self.calls.append((url, json, timeout))
        if isinstance(self._behavior, Exception):
            raise self._behavior
        return _FakeResponse(self._behavior, text=f"status={self._behavior}")


def _make_reporter(
    tmp_dir: Path,
    *,
    pending: int = 0,
    watchdog_payload: dict | None = None,
    behavior=200,
    lifecycle_data: dict | None = None,
):
    """统一 reporter 构造（lifecycle 落 tmp 目录，互不干扰）。"""
    lc_path = tmp_dir / "rc.json"
    if lifecycle_data is not None:
        lc_path.write_text(json.dumps(lifecycle_data), encoding="utf-8")
    lc = LifecycleRecorder(path=lc_path)

    buf = _FakeBuffer(pending) if pending or pending == 0 else None
    # 默认开 buffer；如果传 pending=None 显式禁用
    if pending is None:
        buf = None

    wd = _FakeWatchdog(watchdog_payload) if watchdog_payload else None

    return HealthReporter(
        HealthConfig(),
        lc,
        machine_id="MACHINE_TEST",
        hostname="HOST_TEST",
        employee_name="",
        url="http://test.local/api/v1/agent/health",
        request_timeout_seconds=5,
        session=_FakeSession(behavior),
        watchdog=wd,
        window_buffer=buf,
    )


# ---------------------------------------------------------------------------
# Version
# ---------------------------------------------------------------------------

def v1_agent_version_nonempty():
    assert isinstance(AGENT_VERSION, str) and len(AGENT_VERSION) > 0
    # 形如 X.Y.Z
    parts = AGENT_VERSION.split(".")
    assert len(parts) == 3, f"AGENT_VERSION should be X.Y.Z, got {AGENT_VERSION!r}"


case("V1 AGENT_VERSION is non-empty 'X.Y.Z' string", v1_agent_version_nonempty)


# ---------------------------------------------------------------------------
# HealthConfig
# ---------------------------------------------------------------------------

def c1_health_config_defaults():
    cfg = HealthConfig()
    assert cfg.enabled is True
    assert cfg.upload_interval_seconds == 120, cfg.upload_interval_seconds
    assert cfg.health_api_path == "/api/v1/agent/health"
    assert isinstance(cfg.upload_backoff_seconds, list) and cfg.upload_backoff_seconds


case("C1 HealthConfig defaults match design (120s, /agent/health)", c1_health_config_defaults)


# ---------------------------------------------------------------------------
# build_payload
# ---------------------------------------------------------------------------

def r1_payload_fields_complete():
    with tempfile.TemporaryDirectory() as tmp:
        reporter = _make_reporter(Path(tmp), pending=42, watchdog_payload={
            "enabled": True,
            "thresholds_seconds": {"tracker": 90, "uploader": 360, "agent_loop": 180},
            "ages_seconds": {"tracker": 1.0, "uploader": 10.0, "agent_loop": 5.0},
            "misses": {"tracker": 0, "uploader": 0, "agent_loop": 0},
        })
        payload = reporter.build_payload()
    required = {
        "machine_id", "hostname", "employee_name", "agent_version",
        "process_started_at", "uptime_seconds",
        "first_start_at", "last_start_at", "last_exit_at", "last_exit_reason",
        "restart_count", "pending_events", "watchdog",
    }
    missing = required - set(payload.keys())
    assert not missing, f"missing keys: {missing}"
    assert payload["machine_id"] == "MACHINE_TEST"
    assert payload["agent_version"] == AGENT_VERSION
    assert payload["pending_events"] == 42
    assert isinstance(payload["uptime_seconds"], int)


def r2_pending_from_buffer():
    with tempfile.TemporaryDirectory() as tmp:
        reporter = _make_reporter(Path(tmp), pending=7)
        payload = reporter.build_payload()
    assert payload["pending_events"] == 7


def r3_no_buffer_pending_zero():
    with tempfile.TemporaryDirectory() as tmp:
        reporter = _make_reporter(Path(tmp), pending=None)
        payload = reporter.build_payload()
    assert payload["pending_events"] == 0


def r4_no_watchdog_null():
    with tempfile.TemporaryDirectory() as tmp:
        reporter = _make_reporter(Path(tmp), watchdog_payload=None)
        payload = reporter.build_payload()
    assert payload["watchdog"] is None


def r5_watchdog_passthrough():
    wd = {
        "enabled": True,
        "thresholds_seconds": {"tracker": 90, "uploader": 360, "agent_loop": 180},
        "ages_seconds": {"tracker": 1.5, "uploader": 12.0, "agent_loop": 8.0},
        "misses": {"tracker": 0, "uploader": 1, "agent_loop": 0},
    }
    with tempfile.TemporaryDirectory() as tmp:
        reporter = _make_reporter(Path(tmp), watchdog_payload=wd)
        payload = reporter.build_payload()
    assert payload["watchdog"] == wd


def r6_no_lifecycle_file_tolerated():
    with tempfile.TemporaryDirectory() as tmp:
        reporter = _make_reporter(Path(tmp))  # lifecycle_data=None → file 不存在
        payload = reporter.build_payload()
    assert payload["first_start_at"] is None
    assert payload["last_start_at"] is None
    assert payload["restart_count"] == 0


def r7_watchdog_exit_reason_passthrough():
    data = {
        "version": "0.5.2",
        "restart_count": 5,
        "first_start_at": "2026-05-26T10:00:00Z",
        "last_start_at": "2026-05-30T20:00:00Z",
        "last_exit_at": "2026-05-30T19:59:30Z",
        "last_exit_reason": "watchdog_tracker_timeout",
    }
    with tempfile.TemporaryDirectory() as tmp:
        reporter = _make_reporter(Path(tmp), lifecycle_data=data)
        payload = reporter.build_payload()
    assert payload["last_exit_reason"] == "watchdog_tracker_timeout"
    assert payload["restart_count"] == 5
    assert payload["first_start_at"] == "2026-05-26T10:00:00Z"


def r8_uptime_monotonic():
    with tempfile.TemporaryDirectory() as tmp:
        reporter = _make_reporter(Path(tmp))
        a = reporter.build_payload()["uptime_seconds"]
        import time as _t
        _t.sleep(0.05)
        b = reporter.build_payload()["uptime_seconds"]
    assert b >= a, f"uptime regressed: {a} -> {b}"


case("R1 build_payload contains all server-required fields", r1_payload_fields_complete)
case("R2 pending_events read from buffer.stats()['pending']", r2_pending_from_buffer)
case("R3 buffer=None → pending_events=0", r3_no_buffer_pending_zero)
case("R4 watchdog=None → payload['watchdog'] is None", r4_no_watchdog_null)
case("R5 watchdog snapshot passes through to payload", r5_watchdog_passthrough)
case("R6 missing lifecycle file → fields are None/0, no exception", r6_no_lifecycle_file_tolerated)
case("R7 lifecycle last_exit_reason passes through (watchdog timeout case)", r7_watchdog_exit_reason_passthrough)
case("R8 uptime_seconds monotonically non-decreasing", r8_uptime_monotonic)


# ---------------------------------------------------------------------------
# _report_once with fake session
# ---------------------------------------------------------------------------

def n1_200_ok():
    with tempfile.TemporaryDirectory() as tmp:
        r = _make_reporter(Path(tmp), behavior=200)
        assert r._report_once() is True


def n2_500_returns_false():
    with tempfile.TemporaryDirectory() as tmp:
        r = _make_reporter(Path(tmp), behavior=500)
        assert r._report_once() is False


def n3_422_returns_false():
    with tempfile.TemporaryDirectory() as tmp:
        r = _make_reporter(Path(tmp), behavior=422)
        assert r._report_once() is False


def n4_network_error_returns_false():
    with tempfile.TemporaryDirectory() as tmp:
        r = _make_reporter(Path(tmp), behavior=requests.ConnectionError("boom"))
        assert r._report_once() is False


case("N1 POST 200 → True", n1_200_ok)
case("N2 POST 500 → False (server error, retry)", n2_500_returns_false)
case("N3 POST 422 → False (schema error, log+retry)", n3_422_returns_false)
case("N4 RequestException → False (network)", n4_network_error_returns_false)


# ---------------------------------------------------------------------------
# 入口
# ---------------------------------------------------------------------------
print()
print(f"{_pass} passed, {_fail} failed")
sys.exit(0 if _fail == 0 else 1)

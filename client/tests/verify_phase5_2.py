"""Phase 5.2 · Watchdog + Heartbeat + Lifecycle 单元验证。

跑法（在 client/）::

    python tests/verify_phase5_2.py

风格与 [verify_phase5_1.py] 一致：手写 case() 入口，不依赖 pytest /
unittest discovery。所有 I/O 集中到 tmpdir 避免污染真实 ProgramData。

覆盖：
  Heartbeat ------------------------------------------------------------
    H1  未 tick 时 age = now - birth
    H2  tick(name) 后 age = now - tick_ts
    H3  多个 worker 互不影响

  Watchdog._check_once  ------------------------------------------------
    W1  全部新鲜 → 返回 None，无 miss
    W2  单一 worker 老旧 1 次 → miss=1，仍返回 None（D2 决策）
    W3  单一 worker 连续 2 次老旧 → 返回 worker name
    W4  worker 老旧后恢复 → miss 归零
    W5  多 worker 同时老旧 → 取 dict 顺序第一个触发

  Watchdog._trigger_exit / _mono_to_iso / _write_heartbeat_file -------
    W6  _trigger_exit 调 lifecycle.record_exit + exit_callable，
        reason 命名为 watchdog_<worker>_timeout (D6)
    W7  _mono_to_iso 反推 wall clock 在合理误差内
    W8  _write_heartbeat_file 写出可 JSON parse、字段齐全的文件

  Lifecycle -----------------------------------------------------------
    L1  record_start 首次 → restart_count=1, last_exit_reason=initial
    L2  连续 record_start 两次 → restart_count=2，first_start_at 保留
    L3  record_exit 后再 record_start → last_exit_reason 保留可见
    L4  损坏文件容错：读到非法 JSON 视同空记录
"""
from __future__ import annotations

import json
import sys
import tempfile
from pathlib import Path

try:
    sys.stdout.reconfigure(encoding="utf-8")  # type: ignore[attr-defined]
    sys.stderr.reconfigure(encoding="utf-8")  # type: ignore[attr-defined]
except Exception:  # noqa: BLE001
    pass

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from app.config import WatchdogConfig  # noqa: E402
from app.lifecycle import (  # noqa: E402
    EXIT_REASON_INITIAL,
    EXIT_REASON_NORMAL,
    LifecycleRecorder,
)
from app.watchdog import (  # noqa: E402
    WORKER_AGENT_LOOP,
    WORKER_TRACKER,
    WORKER_UPLOADER,
    Heartbeat,
    Watchdog,
)

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
# Fake clock helpers
# ---------------------------------------------------------------------------

class FakeClock:
    """可控的 monotonic 时钟，单测里 tick 时间任意推。"""

    def __init__(self, start: float = 1000.0) -> None:
        self._t = start

    def __call__(self) -> float:
        return self._t

    def advance(self, secs: float) -> None:
        self._t += secs


# ---------------------------------------------------------------------------
# Heartbeat tests
# ---------------------------------------------------------------------------

def h1_never_ticked_uses_birth():
    clk = FakeClock(start=100.0)
    hb = Heartbeat(clock=clk)
    clk.advance(7)
    assert abs(hb.age("never_ticked", clk()) - 7) < 1e-6, (
        f"expected age≈7, got {hb.age('never_ticked', clk())}"
    )


def h2_tick_resets_age():
    clk = FakeClock(start=100.0)
    hb = Heartbeat(clock=clk)
    clk.advance(5)
    hb.tick("w")
    clk.advance(3)
    assert abs(hb.age("w", clk()) - 3) < 1e-6


def h3_workers_independent():
    clk = FakeClock(start=100.0)
    hb = Heartbeat(clock=clk)
    clk.advance(2)
    hb.tick("a")
    clk.advance(10)
    hb.tick("b")
    clk.advance(1)
    now = clk()
    assert abs(hb.age("a", now) - 11) < 1e-6, hb.age("a", now)
    assert abs(hb.age("b", now) - 1) < 1e-6, hb.age("b", now)


case("H1 never-ticked worker uses birth", h1_never_ticked_uses_birth)
case("H2 tick(name) resets age to 0", h2_tick_resets_age)
case("H3 multiple workers tracked independently", h3_workers_independent)


# ---------------------------------------------------------------------------
# Watchdog._check_once tests
# ---------------------------------------------------------------------------

def _make_watchdog(clk: FakeClock, *, lifecycle=None, exit_calls=None,
                   thresholds=(30, 60, 90), miss_to_exit=2,
                   heartbeat_file=None):
    """组装一个 watchdog 注入了 fake clock + 可观察的 exit callable。"""
    config = WatchdogConfig(
        enabled=True,
        check_interval_seconds=1,
        tracker_threshold_seconds=thresholds[0],
        uploader_threshold_seconds=thresholds[1],
        agent_loop_threshold_seconds=thresholds[2],
        miss_count_to_exit=miss_to_exit,
        heartbeat_file_throttle_seconds=1,
    )
    hb = Heartbeat(clock=clk)
    lifecycle = lifecycle or _DummyLifecycle()
    exit_calls = exit_calls if exit_calls is not None else []

    def fake_exit(code):  # noqa: ANN001
        exit_calls.append(code)
        # 抛而非真退，否则单测进程也跟着死
        raise SystemExit(code)

    wd = Watchdog(
        config, hb, lifecycle,
        heartbeat_file_path=heartbeat_file,
        exit_callable=fake_exit,
        clock=clk,
        wall_clock=lambda: 1_700_000_000.0,  # 固定 wall anchor
    )
    return wd, hb, lifecycle, exit_calls


class _DummyLifecycle:
    def __init__(self) -> None:
        self.records: list[tuple[str, str]] = []

    def record_exit(self, reason: str) -> None:
        self.records.append(("exit", reason))


def w1_all_fresh_returns_none():
    clk = FakeClock(start=1000.0)
    wd, hb, _, _ = _make_watchdog(clk)
    # 三个 worker 都立刻 tick
    hb.tick(WORKER_TRACKER)
    hb.tick(WORKER_UPLOADER)
    hb.tick(WORKER_AGENT_LOOP)
    clk.advance(5)
    assert wd._check_once(clk()) is None
    assert wd._misses == {WORKER_TRACKER: 0, WORKER_UPLOADER: 0, WORKER_AGENT_LOOP: 0}


def w2_one_miss_returns_none():
    clk = FakeClock(start=1000.0)
    wd, hb, _, _ = _make_watchdog(clk, thresholds=(30, 60, 90))
    hb.tick(WORKER_TRACKER)
    # tracker 阈值 30，过 40s 没 tick → 1 次 miss
    clk.advance(40)
    triggered = wd._check_once(clk())
    assert triggered is None, f"D2: 1 miss must not trigger, got {triggered}"
    assert wd._misses[WORKER_TRACKER] == 1


def w3_two_consecutive_misses_triggers():
    clk = FakeClock(start=1000.0)
    wd, hb, _, _ = _make_watchdog(clk, thresholds=(30, 60, 90), miss_to_exit=2)
    hb.tick(WORKER_TRACKER)
    clk.advance(40)
    assert wd._check_once(clk()) is None  # miss=1
    clk.advance(40)
    triggered = wd._check_once(clk())  # miss=2 → trigger
    assert triggered == WORKER_TRACKER, triggered


def w4_recovery_resets_misses():
    clk = FakeClock(start=1000.0)
    wd, hb, _, _ = _make_watchdog(clk, thresholds=(30, 60, 90))
    hb.tick(WORKER_TRACKER)
    clk.advance(40)
    wd._check_once(clk())  # miss=1
    assert wd._misses[WORKER_TRACKER] == 1
    # tracker 恢复 tick
    hb.tick(WORKER_TRACKER)
    clk.advance(5)
    assert wd._check_once(clk()) is None
    assert wd._misses[WORKER_TRACKER] == 0, "recovery must zero miss count"


def w5_first_stale_worker_triggers():
    clk = FakeClock(start=1000.0)
    wd, hb, _, _ = _make_watchdog(clk, thresholds=(30, 30, 30), miss_to_exit=1)
    # 完全不 tick 任何 worker；用 birth 算 age
    clk.advance(40)
    triggered = wd._check_once(clk())
    # dict 顺序 = TRACKER, UPLOADER, AGENT_LOOP（按 _thresholds 初始化顺序）
    assert triggered == WORKER_TRACKER, triggered


case("W1 all workers fresh → no trigger, no miss", w1_all_fresh_returns_none)
case("W2 single miss → returns None (D2 needs 2)", w2_one_miss_returns_none)
case("W3 two consecutive misses → trigger", w3_two_consecutive_misses_triggers)
case("W4 worker recovery → miss count resets", w4_recovery_resets_misses)
case("W5 first stale worker in dict order triggers", w5_first_stale_worker_triggers)


# ---------------------------------------------------------------------------
# Watchdog._trigger_exit / _mono_to_iso / _write_heartbeat_file
# ---------------------------------------------------------------------------

def w6_trigger_exit_calls_lifecycle_and_exit():
    clk = FakeClock(start=1000.0)
    lc = _DummyLifecycle()
    exit_calls: list[int] = []
    wd, hb, _, _ = _make_watchdog(
        clk, lifecycle=lc, exit_calls=exit_calls,
        thresholds=(30, 30, 30), miss_to_exit=1,
    )
    clk.advance(50)
    try:
        wd._trigger_exit(WORKER_UPLOADER)
    except SystemExit:
        pass  # fake_exit raises SystemExit
    assert lc.records == [("exit", "watchdog_uploader_timeout")], lc.records
    assert exit_calls == [Watchdog.EXIT_CODE], exit_calls


def w7_mono_to_iso_reasonable():
    clk = FakeClock(start=1000.0)
    wd, _, _, _ = _make_watchdog(clk)
    # mono_anchor = 1000，wall_anchor = 1_700_000_000；mono=1030 → wall=1_700_000_030
    iso = wd._mono_to_iso(1030.0)
    # 1_700_000_030 unix = 2023-11-14T22:13:50Z
    assert iso == "2023-11-14T22:13:50Z", iso


def w8_write_heartbeat_file_schema():
    clk = FakeClock(start=1000.0)
    with tempfile.TemporaryDirectory() as tmp:
        hb_file = Path(tmp) / "heartbeat.json"
        wd, hb, _, _ = _make_watchdog(clk, heartbeat_file=hb_file)
        hb.tick(WORKER_TRACKER)
        clk.advance(10)
        wd._write_heartbeat_file(clk())
        assert hb_file.exists()
        data = json.loads(hb_file.read_text(encoding="utf-8"))
        required = {
            "version", "last_check_at", "uptime_seconds",
            "thresholds_seconds", "miss_count_to_exit",
            "ages_seconds", "ticks_iso", "misses",
        }
        missing = required - set(data.keys())
        assert not missing, f"missing keys: {missing}"
        assert WORKER_TRACKER in data["ticks_iso"]
        assert abs(data["ages_seconds"][WORKER_TRACKER] - 10) < 0.5, (
            data["ages_seconds"]
        )


case("W6 _trigger_exit writes lifecycle + calls os._exit", w6_trigger_exit_calls_lifecycle_and_exit)
case("W7 _mono_to_iso reverse-derives wall ISO", w7_mono_to_iso_reasonable)
case("W8 _write_heartbeat_file produces valid JSON with all fields",
     w8_write_heartbeat_file_schema)


# ---------------------------------------------------------------------------
# Lifecycle tests
# ---------------------------------------------------------------------------

def l1_first_start():
    with tempfile.TemporaryDirectory() as tmp:
        lc = LifecycleRecorder(path=Path(tmp) / "rc.json")
        p = lc.record_start()
        assert p["restart_count"] == 1, p
        assert p["last_exit_reason"] == EXIT_REASON_INITIAL, p
        assert p["last_exit_at"] is None, p
        assert p["first_start_at"] == p["last_start_at"], p


def l2_two_starts_accumulate():
    with tempfile.TemporaryDirectory() as tmp:
        lc = LifecycleRecorder(path=Path(tmp) / "rc.json")
        p1 = lc.record_start()
        # 模拟"两秒后又起一次"——文件落盘后再调一次 record_start
        p2 = lc.record_start()
        assert p2["restart_count"] == 2, p2
        # first_start_at 不变
        assert p2["first_start_at"] == p1["first_start_at"]


def l3_exit_then_start_preserves_reason():
    with tempfile.TemporaryDirectory() as tmp:
        path = Path(tmp) / "rc.json"
        lc = LifecycleRecorder(path=path)
        lc.record_start()
        lc.record_exit("watchdog_tracker_timeout")
        p3 = lc.record_start()
        # 启动后能看到上次为啥死的
        assert p3["last_exit_reason"] == "watchdog_tracker_timeout", p3
        assert p3["restart_count"] == 2, p3


def l4_corrupted_json_tolerated():
    with tempfile.TemporaryDirectory() as tmp:
        path = Path(tmp) / "rc.json"
        path.write_text("{not valid json", encoding="utf-8")
        lc = LifecycleRecorder(path=path)
        p = lc.record_start()
        # 当作空记录从头来
        assert p["restart_count"] == 1, p
        # 写回去的文件必须是合法 JSON
        round_trip = json.loads(path.read_text(encoding="utf-8"))
        assert round_trip["restart_count"] == 1, round_trip


case("L1 first start → restart_count=1, reason=initial", l1_first_start)
case("L2 two starts → restart_count=2, first_start preserved", l2_two_starts_accumulate)
case("L3 exit then start → last_exit_reason visible", l3_exit_then_start_preserves_reason)
case("L4 corrupted JSON → treated as empty + recovered", l4_corrupted_json_tolerated)


# ---------------------------------------------------------------------------
# 入口
# ---------------------------------------------------------------------------
print()
print(f"{_pass} passed, {_fail} failed")
sys.exit(0 if _fail == 0 else 1)

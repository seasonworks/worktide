"""Phase 5.2 · 分析 lag_samples.ndjson，输出 max / P95 / P99 + 阈值健康度。

跑法（在 client/）::

    python tools/lag_analyzer.py

输出每个 worker 的：
  - max / P95 / P99 lag（秒）
  - 阈值（从 config.watchdog 读取）
  - 余量百分比（vs threshold）
  - 累计 max miss count（>0 = 曾发生过 stale 检测）
  - status: OK / WARN / ALERT

判定：
  OK    : max < 80% threshold AND max_miss == 0
  WARN  : max >= 80% threshold（接近但没超）or max_miss > 0 但 < miss_to_exit
  ALERT : max >= threshold（超过阈值，理论上 watchdog 会触发；如果还在跑说明
          阈值偏宽，或刚好压在 threshold 上下抖动）
"""
from __future__ import annotations

import json
import sys
from pathlib import Path
from statistics import quantiles

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from app.config import data_dir, load_config  # noqa: E402

SAMPLE_FILE = data_dir() / "state" / "lag_samples.ndjson"

WORKERS = ("tracker", "uploader", "agent_loop")


def _percentile(vals: list[float], pct: float) -> float:
    """``statistics.quantiles`` n=100 算的是 1..99 分位数；fall back 到 max
    当样本太少。"""
    if not vals:
        return 0.0
    if len(vals) < 2:
        return vals[0]
    # method="inclusive" 在样本量 < 100 时也给得出 P95/P99
    qs = quantiles(vals, n=100, method="inclusive")
    idx = max(0, min(len(qs) - 1, int(pct) - 1))
    return qs[idx]


def _status(vmax: float, threshold: float, max_miss: int,
            miss_to_exit: int) -> str:
    if vmax >= threshold:
        return "ALERT"
    if vmax >= threshold * 0.8 or 0 < max_miss < miss_to_exit:
        return "WARN"
    if max_miss >= miss_to_exit:
        return "ALERT"
    return "OK"


def main() -> None:
    if not SAMPLE_FILE.exists():
        print(f"no samples file yet: {SAMPLE_FILE}")
        sys.exit(1)

    config = load_config()
    cfg = config.watchdog
    thresholds = {
        "tracker": cfg.tracker_threshold_seconds,
        "uploader": cfg.uploader_threshold_seconds,
        "agent_loop": cfg.agent_loop_threshold_seconds,
    }
    miss_to_exit = cfg.miss_count_to_exit

    series: dict[str, list[float]] = {k: [] for k in WORKERS}
    max_miss: dict[str, int] = {k: 0 for k in WORKERS}
    n_samples = 0
    first_wall = None
    last_wall = None

    with SAMPLE_FILE.open(encoding="utf-8") as f:
        for line in f:
            line = line.strip()
            if not line:
                continue
            n_samples += 1
            d = json.loads(line)
            if first_wall is None:
                first_wall = d.get("wall_at")
            last_wall = d.get("wall_at")
            for k in WORKERS:
                v = d.get("ages_seconds", {}).get(k)
                if v is not None:
                    series[k].append(float(v))
                m = int(d.get("misses", {}).get(k, 0))
                if m > max_miss[k]:
                    max_miss[k] = m

    print(f"samples       : {n_samples}")
    print(f"first sample  : {first_wall}")
    print(f"last sample   : {last_wall}")
    print(f"miss_to_exit  : {miss_to_exit}")
    print()
    print(
        f"{'worker':<12} {'max':>8} {'p95':>8} {'p99':>8}"
        f" {'thr':>6} {'max%':>6} {'miss':>5}  status"
    )
    print("-" * 72)
    for k in WORKERS:
        vals = series[k]
        if not vals:
            print(f"{k:<12}  (no samples)")
            continue
        vmax = max(vals)
        p95 = _percentile(vals, 95)
        p99 = _percentile(vals, 99)
        thr = thresholds[k]
        pct = (vmax / thr * 100) if thr else 0
        status = _status(vmax, thr, max_miss[k], miss_to_exit)
        print(
            f"{k:<12} {vmax:>8.1f} {p95:>8.1f} {p99:>8.1f}"
            f" {thr:>6d} {pct:>5.0f}% {max_miss[k]:>5d}  {status}"
        )


if __name__ == "__main__":
    main()

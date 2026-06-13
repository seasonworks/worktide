"""Phase 5.2 · 实地采样 watchdog heartbeat ages，写 ndjson 供后期统计。

跑法（在 client/）::

    python tools/lag_collector.py

源码运行时数据写到 ``client/state/lag_samples.ndjson``；
打包后 frozen 时写到 ``%PROGRAMDATA%/EmployeeAgent/state/lag_samples.ndjson``。

只在 ``heartbeat.json`` 的 ``last_check_at`` 变化时采样一次，避免重复点。
worker max / P95 / P99 统计由 [lag_analyzer.py] 离线算。
"""
from __future__ import annotations

import json
import sys
import time
from datetime import datetime, timezone
from pathlib import Path

# 让 tools/ 能 import app.config
sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from app.config import data_dir  # noqa: E402

INTERVAL_SECONDS = 10

SAMPLE_FILE = data_dir() / "state" / "lag_samples.ndjson"
HEARTBEAT_FILE = data_dir() / "state" / "heartbeat.json"


def main() -> None:
    SAMPLE_FILE.parent.mkdir(parents=True, exist_ok=True)
    print(f"[lag_collector] interval={INTERVAL_SECONDS}s")
    print(f"  hb in    : {HEARTBEAT_FILE}")
    print(f"  samples  : {SAMPLE_FILE}")
    sys.stdout.flush()

    last_check_at = None
    while True:
        try:
            if HEARTBEAT_FILE.exists():
                data = json.loads(HEARTBEAT_FILE.read_text(encoding="utf-8"))
                check_at = data.get("last_check_at")
                # 只在 watchdog 实际更新心跳文件时记录一行（避免重复同一帧）
                if check_at != last_check_at:
                    sample = {
                        "wall_at": datetime.now(timezone.utc).strftime(
                            "%Y-%m-%dT%H:%M:%SZ"
                        ),
                        "hb_last_check_at": check_at,
                        "ages_seconds": data.get("ages_seconds", {}),
                        "misses": data.get("misses", {}),
                        "uptime_seconds": data.get("uptime_seconds", 0),
                    }
                    with SAMPLE_FILE.open("a", encoding="utf-8") as f:
                        f.write(json.dumps(sample, ensure_ascii=False) + "\n")
                    last_check_at = check_at
        except Exception as e:  # noqa: BLE001
            # 永远不死：观察工具断了不该影响 agent 验证
            print(f"[lag_collector] error: {e}", flush=True)
        time.sleep(INTERVAL_SECONDS)


if __name__ == "__main__":
    main()

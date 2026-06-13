"""独立 2 小时（默认 7200s）soak 脚本。

为什么单独存在：联调脚本是单进程跑完即出报告，运行 2h 不合适；本文件可被用户
单独运行：

    cd client/
    python tests\integration\soak_2h.py             # 默认 7200s = 2h
    python tests\integration\soak_2h.py --soak=3600 # 自定义 1h
    python tests\integration\soak_2h.py --rate=1    # 每秒 1 条（接近真实速率）
    python tests\integration\soak_2h.py --csv=soak.csv  # 同时落 CSV 便于后处理

监控指标：
- CPU%（每 5s 一拍）
- RSS（每 5s 一拍）
- 线程数
- buffer pending / uploaded / invalid
- 服务端 raw 计数
- uploader 累计 sub_batches（POST 次数）

终止判定（无泄漏）：
- 后半段（Q3 + Q4）RSS 净增量 <= 前半段（Q1 + Q2）+ 10MB 容差
- 线程数稳定（稳态 max - min <= 2）
- 数据零丢失：server raw == buffer.uploaded
"""
from __future__ import annotations

import csv
import gc
import sys
import tempfile
import threading
import time
import uuid
from datetime import datetime, timedelta, timezone
from pathlib import Path

try:
    sys.stdout.reconfigure(encoding="utf-8")  # type: ignore[attr-defined]
except Exception:  # noqa: BLE001
    pass

sys.path.insert(0, str(Path(__file__).resolve().parents[2]))

from app.config import WindowTrackingConfig  # noqa: E402
from app.window_buffer import (  # noqa: E402
    STATUS_INVALID, STATUS_PENDING, STATUS_UPLOADED, WindowBuffer,
)
from app.window_uploader import WindowUploader  # noqa: E402
from tests.integration.harness import (  # noqa: E402
    IsolatedServer, clock_in, count_raw, pick_free_port, register_machine,
)


def _iso(dt):
    return dt.astimezone(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def main():
    duration = 7200          # 2 小时
    events_per_sec = 0.5     # 接近真实采样的 event 率（tracker ~0.5/s）
    csv_path: Path | None = None
    for a in sys.argv[1:]:
        if a.startswith("--soak="):
            duration = int(a.split("=", 1)[1])
        elif a.startswith("--rate="):
            events_per_sec = float(a.split("=", 1)[1])
        elif a.startswith("--csv="):
            csv_path = Path(a.split("=", 1)[1]).resolve()

    try:
        import psutil  # type: ignore
        proc = psutil.Process()
        have_psutil = True
    except ImportError:
        have_psutil = False
        proc = None
        print("[warn] psutil 未安装，将仅监控 buffer/server 计数（建议 pip install psutil）")

    print(f"[soak] duration={duration}s rate={events_per_sec} events/s csv={csv_path}")

    with tempfile.TemporaryDirectory(ignore_cleanup_errors=True) as tmp:
        tmp_path = Path(tmp)
        db_path = tmp_path / "soak.db"
        port = pick_free_port(9130)
        machine_id = f"SOAK-{uuid.uuid4().hex[:8]}"

        with IsolatedServer(db_path, port) as srv:
            print(f"[soak] server up url={srv.url}")
            emp = register_machine(srv.api, machine_id, name="2h-soak")
            clock_in(srv.api, emp)

            buf = WindowBuffer(tmp_path / "buffer.db")
            cfg = WindowTrackingConfig(
                upload_batch_size=500,
                upload_interval_seconds=30,
                upload_backoff_seconds=[5, 30, 120, 300],
                upload_max_attempts=5,
            )
            uploader = WindowUploader(
                cfg, buf,
                machine_id=machine_id,
                url=f"{srv.api}/windows/report",
                request_timeout_seconds=10,
            )
            uploader.start()
            if have_psutil:
                proc.cpu_percent(interval=None)  # baseline

            stop_evt = threading.Event()
            samples: list[dict] = []

            def producer():
                i = 0
                base = datetime.now(timezone.utc)
                interval = 1.0 / max(0.01, events_per_sec)
                while not stop_evt.is_set():
                    buf.append_event(
                        client_event_id=uuid.uuid4().hex,
                        process_name=f"app{i % 5}.exe",
                        window_title=f"T-{i % 100}",
                        started_at=_iso(base + timedelta(seconds=i)),
                        ended_at=_iso(base + timedelta(seconds=i + 1)),
                        duration_seconds=1,
                        had_input=True,
                    )
                    i += 1
                    if stop_evt.wait(interval):
                        break

            def sampler():
                while not stop_evt.is_set():
                    row = {
                        "t": time.monotonic(),
                        "buf_pending": buf.stats()[STATUS_PENDING],
                        "buf_uploaded": buf.stats()[STATUS_UPLOADED],
                    }
                    if have_psutil:
                        with proc.oneshot():
                            row["cpu"] = proc.cpu_percent(interval=None)
                            row["rss_mb"] = proc.memory_info().rss / 1024 / 1024
                            row["threads"] = proc.num_threads()
                    samples.append(row)
                    if stop_evt.wait(5):
                        break

            prod_th = threading.Thread(target=producer, daemon=True, name="soak-producer")
            samp_th = threading.Thread(target=sampler, daemon=True, name="soak-sampler")
            prod_th.start()
            samp_th.start()

            print("[soak] running...")
            t_start = time.monotonic()
            try:
                while time.monotonic() - t_start < duration:
                    time.sleep(60)
                    elapsed = int(time.monotonic() - t_start)
                    s = buf.stats()
                    last = samples[-1] if samples else {}
                    print(
                        f"  +{elapsed:>5}s  buf={s[STATUS_PENDING]:>4}p/{s[STATUS_UPLOADED]:>6}u"
                        + (f"  cpu={last.get('cpu',0):>4.1f}%  rss={last.get('rss_mb',0):>5.1f}MB"
                           f"  th={last.get('threads','?')}" if have_psutil else "")
                    )
            except KeyboardInterrupt:
                print("[soak] interrupted")

            stop_evt.set()
            prod_th.join(timeout=10)
            uploader.stop()
            samp_th.join(timeout=10)
            gc.collect()

            # 分析
            n_raw = count_raw(db_path, emp)
            s_final = buf.stats()
            print(f"\n[soak] buffer final: pending={s_final[STATUS_PENDING]}"
                  f" uploaded={s_final[STATUS_UPLOADED]} invalid={s_final[STATUS_INVALID]}")
            print(f"[soak] server raw: {n_raw}")

            if csv_path and samples:
                cols = ["t", "buf_pending", "buf_uploaded", "cpu", "rss_mb", "threads"]
                with csv_path.open("w", newline="", encoding="utf-8") as f:
                    w = csv.DictWriter(f, fieldnames=cols, extrasaction="ignore")
                    w.writeheader()
                    for r in samples:
                        w.writerow(r)
                print(f"[soak] csv saved: {csv_path}")

            failures: list[str] = []

            if have_psutil and len(samples) >= 8:
                rss = [r["rss_mb"] for r in samples]
                threads = [r["threads"] for r in samples]
                n = len(samples)
                q1, q2, q3 = n // 4, n // 2, 3 * n // 4
                quarters = [rss[:q1], rss[q1:q2], rss[q2:q3], rss[q3:]]
                growth = [(q[-1] - q[0]) if q else 0.0 for q in quarters]
                first_half = growth[0] + growth[1]
                second_half = growth[2] + growth[3]
                print(f"[soak] rss quarter growth (MB): Q1={growth[0]:.1f} Q2={growth[1]:.1f} "
                      f"Q3={growth[2]:.1f} Q4={growth[3]:.1f}")
                print(f"[soak] first-half {first_half:.1f}MB / second-half {second_half:.1f}MB")
                print(f"[soak] rss full: min={min(rss):.1f} max={max(rss):.1f}")
                steady = threads[q2:]
                print(f"[soak] threads steady: min={min(steady)} max={max(steady)}")

                if second_half > first_half + 10:
                    failures.append(
                        f"无泄漏判定失败：second_half={second_half:.1f}MB > "
                        f"first_half={first_half:.1f}MB + 10 容差"
                    )
                if max(steady) - min(steady) > 2:
                    failures.append(
                        f"线程数不稳定：steady min={min(steady)} max={max(steady)}"
                    )

            if n_raw != s_final[STATUS_UPLOADED]:
                failures.append(
                    f"数据丢失：server raw={n_raw} != buffer uploaded={s_final[STATUS_UPLOADED]}"
                )

            print()
            if failures:
                print("[soak] FAIL：")
                for f in failures:
                    print(f"  - {f}")
                return 1
            print("[soak] PASS：无泄漏 / 线程稳定 / 零丢失")
            return 0


if __name__ == "__main__":
    sys.exit(main())

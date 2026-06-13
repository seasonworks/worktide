"""快速冒烟：验证 harness 能拉起隔离 server、注册员工、走通一次 /windows/report。"""
from __future__ import annotations

import sys
import tempfile
import uuid
from datetime import datetime, timedelta, timezone
from pathlib import Path

try:
    sys.stdout.reconfigure(encoding="utf-8")  # type: ignore[attr-defined]
    sys.stderr.reconfigure(encoding="utf-8")  # type: ignore[attr-defined]
except Exception:  # noqa: BLE001
    pass

sys.path.insert(0, str(Path(__file__).resolve().parents[2]))

from app.config import WindowTrackingConfig  # noqa: E402
from app.window_buffer import WindowBuffer  # noqa: E402
from app.window_uploader import WindowUploader  # noqa: E402
from tests.integration.harness import (  # noqa: E402
    IsolatedServer,
    count_raw,
    count_sessions,
    count_raw_processed,
    pick_free_port,
    register_machine,
)


def main() -> int:
    with tempfile.TemporaryDirectory(ignore_cleanup_errors=True) as tmp:
        tmp_path = Path(tmp)
        db_path = tmp_path / "smoke.db"
        port = pick_free_port(9100)
        machine_id = f"SMOKE-{uuid.uuid4().hex[:8]}"
        print(f"[smoke] db={db_path} port={port} machine={machine_id}")

        with IsolatedServer(db_path, port) as srv:
            print("[smoke] server up")
            emp_id = register_machine(srv.api, machine_id)
            print(f"[smoke] employee_id={emp_id}")

            buf_path = tmp_path / "buffer.db"
            buf = WindowBuffer(buf_path)
            now = datetime.now(timezone.utc).replace(microsecond=0)
            buf.append_event(
                client_event_id=uuid.uuid4().hex,
                process_name="smoke.exe",
                window_title="Smoke",
                started_at=(now - timedelta(seconds=30)).strftime("%Y-%m-%dT%H:%M:%SZ"),
                ended_at=now.strftime("%Y-%m-%dT%H:%M:%SZ"),
                duration_seconds=30,
                had_input=True,
            )
            cfg = WindowTrackingConfig()
            up = WindowUploader(
                cfg, buf,
                machine_id=machine_id,
                url=f"{srv.api}/windows/report",
                request_timeout_seconds=5,
            )
            stats = up.drain_once()
            print(f"[smoke] drain stats={stats}")

            n_raw = count_raw(db_path, emp_id)
            n_proc = count_raw_processed(db_path, emp_id)
            n_sess = count_sessions(db_path, emp_id)
            print(f"[smoke] raw={n_raw} processed={n_proc} sessions={n_sess}")
            assert n_raw == 1, f"raw should be 1, got {n_raw}"
            assert n_proc == 1, f"processed should be 1, got {n_proc}"
            assert n_sess == 1, f"sessions should be 1, got {n_sess}"
    print("[smoke] PASS")
    return 0


if __name__ == "__main__":
    sys.exit(main())

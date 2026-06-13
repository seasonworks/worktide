"""为 admin/tests/detail_card.mjs 提供 4 名员工的种子数据：

- alice：在职，chrome (在岗时长长) + vscode（在岗短）+ notepad，多条 sessions
- bob：在职，对照组
- archived：在职时上传过窗口数据，然后被 archive
- no_data：在职，但**从未**上传过 window event（用于空态验证）

末行打印 JSON：{port, date, alice_id, bob_id, archived_id, no_data_id}
等 stdin EOF 后退出（atexit 收尾 server）。
"""
from __future__ import annotations

import atexit
import json
import sys
import tempfile
import uuid
from datetime import datetime, timedelta, timezone
from pathlib import Path

try:
    sys.stdout.reconfigure(encoding="utf-8")  # type: ignore[attr-defined]
except Exception:  # noqa: BLE001
    pass

sys.path.insert(0, str(Path(__file__).resolve().parents[2]))

from app.config import WindowTrackingConfig  # noqa: E402
from app.window_buffer import WindowBuffer  # noqa: E402
from app.window_uploader import WindowUploader  # noqa: E402
from tests.integration.harness import (  # noqa: E402
    IsolatedServer,
    archive_employee,
    clock_in,
    pick_free_port,
    register_machine,
)


def _iso(dt):
    return dt.astimezone(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def _seed(buf, *, proc, title, started, ended):
    buf.append_event(
        client_event_id=uuid.uuid4().hex,
        process_name=proc, window_title=title,
        started_at=_iso(started), ended_at=_iso(ended),
        duration_seconds=max(1, int((ended - started).total_seconds())),
        had_input=True,
    )


def _flush(buf, mid, api):
    up = WindowUploader(
        WindowTrackingConfig(), buf,
        machine_id=mid, url=f"{api}/windows/report",
        request_timeout_seconds=5,
    )
    up.drain_once()


def main() -> int:
    tmp = tempfile.TemporaryDirectory()
    atexit.register(tmp.cleanup)
    tmp_path = Path(tmp.name)
    port = pick_free_port(9150)
    srv = IsolatedServer(tmp_path / "detail.db", port)
    srv.start()
    atexit.register(srv.stop)

    today = datetime.now(timezone.utc).date().isoformat()
    base = datetime.now(timezone.utc) + timedelta(seconds=1)

    # ── alice（在职 · 多 sessions · chrome.working 远大于 vscode）──
    mid_a = f"D-A-{uuid.uuid4().hex[:6]}"
    alice = register_machine(srv.api, mid_a, name="Alice")
    clock_in(srv.api, alice)
    buf_a = WindowBuffer(tmp_path / "a.db")
    # chrome 在岗 90s（gap < 5s 会被合 1 个 session；服务端 top_apps 按 total_seconds DESC
    # 但我们前端会重排按 working_seconds DESC）
    _seed(buf_a, proc="chrome.exe", title="Docs - Project Plan",
          started=base + timedelta(seconds=0), ended=base + timedelta(seconds=60))
    _seed(buf_a, proc="chrome.exe", title="Docs - Project Plan",
          started=base + timedelta(seconds=65), ended=base + timedelta(seconds=95))
    # vscode 30s
    _seed(buf_a, proc="vscode.exe", title="main.py",
          started=base + timedelta(seconds=100), ended=base + timedelta(seconds=130))
    # notepad 极长 title（验证 ellipsis）
    long_title = "Some-really-extra-long-window-title " * 6  # > 60 chars
    _seed(buf_a, proc="notepad.exe", title=long_title.strip(),
          started=base + timedelta(seconds=135), ended=base + timedelta(seconds=150))
    _flush(buf_a, mid_a, srv.api)

    # ── bob（对照）──
    mid_b = f"D-B-{uuid.uuid4().hex[:6]}"
    bob = register_machine(srv.api, mid_b, name="Bob")
    clock_in(srv.api, bob)
    buf_b = WindowBuffer(tmp_path / "b.db")
    _seed(buf_b, proc="vscode.exe", title="lib.py",
          started=base, ended=base + timedelta(seconds=40))
    _flush(buf_b, mid_b, srv.api)

    # ── archived 员工（在职时写过数据，再 archive）──
    mid_z = f"D-Z-{uuid.uuid4().hex[:6]}"
    arch = register_machine(srv.api, mid_z, name="Zed-Archived")
    clock_in(srv.api, arch)
    buf_z = WindowBuffer(tmp_path / "z.db")
    _seed(buf_z, proc="chrome.exe", title="Past work",
          started=base, ended=base + timedelta(seconds=20))
    _seed(buf_z, proc="vscode.exe", title="hotfix",
          started=base + timedelta(seconds=25),
          ended=base + timedelta(seconds=50))
    _flush(buf_z, mid_z, srv.api)
    archive_employee(srv.api, arch)

    # ── no_data 员工（注册即止；从未上传窗口数据；触发空态）──
    mid_n = f"D-N-{uuid.uuid4().hex[:6]}"
    nodata = register_machine(srv.api, mid_n, name="Newcomer")
    # 故意不 clock_in、不上传 window event

    print(json.dumps({
        "port": port,
        "date": today,
        "alice_id": alice,
        "bob_id": bob,
        "archived_id": arch,
        "no_data_id": nodata,
    }))
    sys.stdout.flush()
    try:
        sys.stdin.read()
    except KeyboardInterrupt:
        pass
    return 0


if __name__ == "__main__":
    sys.exit(main())

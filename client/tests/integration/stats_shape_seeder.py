"""为 admin/tests/stats_shape.mjs 提供"起服 + 注入数据 + 输出 JSON"的种子脚本。

行为：
1. 在一个空闲端口起隔离 server（独立 DB / 独立 logs）
2. 注册 2 名在职员工 + 1 名 archived 员工
3. clock_in 在职员工，写若干 window events，等聚合
4. 把 archived 员工也写入 raw events（仍 200 入库）然后 archive
5. 在 stdout 最后一行打印 JSON：{port, date, employees, archived_employee_id}
6. 不退出（server 留给 node 端测完再被 kill）—— 用 stdin EOF 触发退出

JS 端调用：spawn 此脚本；解析末行 JSON；测完发送 SIGTERM 或关闭子进程。
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


def main() -> int:
    tmp = tempfile.TemporaryDirectory()
    atexit.register(tmp.cleanup)
    tmp_path = Path(tmp.name)
    db_path = tmp_path / "stats_shape.db"
    port = pick_free_port(9140)

    srv = IsolatedServer(db_path, port)
    srv.start()
    atexit.register(srv.stop)

    today = datetime.now(timezone.utc).date().isoformat()
    base = datetime.now(timezone.utc) + timedelta(seconds=1)

    employees = []
    # ── 在职员工 1 ──
    mid1 = f"SHAPE-A-{uuid.uuid4().hex[:6]}"
    emp1 = register_machine(srv.api, mid1, name="Alice")
    clock_in(srv.api, emp1)
    buf1 = WindowBuffer(tmp_path / "buf1.db")
    _seed(buf1, proc="chrome.exe", title="Docs",
          started=base, ended=base + timedelta(seconds=120))
    _seed(buf1, proc="vscode.exe", title="main.py",
          started=base + timedelta(seconds=125),
          ended=base + timedelta(seconds=200))
    up1 = WindowUploader(
        WindowTrackingConfig(), buf1,
        machine_id=mid1, url=f"{srv.api}/windows/report",
        request_timeout_seconds=5,
    )
    up1.drain_once()
    employees.append({"id": emp1, "name": "Alice"})

    # ── 在职员工 2 ──
    mid2 = f"SHAPE-B-{uuid.uuid4().hex[:6]}"
    emp2 = register_machine(srv.api, mid2, name="Bob")
    clock_in(srv.api, emp2)
    buf2 = WindowBuffer(tmp_path / "buf2.db")
    _seed(buf2, proc="vscode.exe", title="lib.py",
          started=base, ended=base + timedelta(seconds=90))
    _seed(buf2, proc="chrome.exe", title="Spec",
          started=base + timedelta(seconds=95),
          ended=base + timedelta(seconds=150))
    up2 = WindowUploader(
        WindowTrackingConfig(), buf2,
        machine_id=mid2, url=f"{srv.api}/windows/report",
        request_timeout_seconds=5,
    )
    up2.drain_once()
    employees.append({"id": emp2, "name": "Bob"})

    # ── archived 员工 ──
    mid3 = f"SHAPE-Z-{uuid.uuid4().hex[:6]}"
    emp3 = register_machine(srv.api, mid3, name="Zed-Archived")
    clock_in(srv.api, emp3)
    buf3 = WindowBuffer(tmp_path / "buf3.db")
    _seed(buf3, proc="notepad.exe", title="todo",
          started=base, ended=base + timedelta(seconds=60))
    up3 = WindowUploader(
        WindowTrackingConfig(), buf3,
        machine_id=mid3, url=f"{srv.api}/windows/report",
        request_timeout_seconds=5,
    )
    up3.drain_once()
    archive_employee(srv.api, emp3)

    # JS 端从 stdout 末行解析
    print(json.dumps({
        "port": port,
        "date": today,
        "employees": employees,
        "archived_employee_id": emp3,
    }))
    sys.stdout.flush()

    # 等到 stdin 关闭再退出（让 node 端的 spawn 用 close stdin 触发收尾）
    try:
        sys.stdin.read()
    except KeyboardInterrupt:
        pass
    return 0


if __name__ == "__main__":
    sys.exit(main())

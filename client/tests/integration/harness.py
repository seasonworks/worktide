"""Step 5 联调专用 harness：在隔离端口/隔离 DB 上拉起 server，提供基本辅助。

不污染生产 DB（server/data/employees.db）；测试 DB 落在调用方给定的 tmp 路径。
"""
from __future__ import annotations

import os
import socket
import subprocess
import sys
import time
from pathlib import Path
from typing import Optional

import requests

REPO_ROOT = Path(__file__).resolve().parents[3]
SERVER_DIR = REPO_ROOT / "server"
# server 有专用 venv：server/.venv/Scripts/python.exe
SERVER_PYTHON = (
    SERVER_DIR / ".venv" / "Scripts" / "python.exe"
    if os.name == "nt"
    else SERVER_DIR / ".venv" / "bin" / "python"
)


def pick_free_port(default: int = 9100) -> int:
    """优先返回 default，被占用则交给 OS 随机分配。"""
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as s:
        try:
            s.bind(("127.0.0.1", default))
            return default
        except OSError:
            s.bind(("127.0.0.1", 0))
            return s.getsockname()[1]


class IsolatedServer:
    """隔离 uvicorn 服务端：自带 cwd=server/、独立 DB、独立 logs。

    用法：
        with IsolatedServer(db_path, port) as srv:
            assert srv.url == f"http://127.0.0.1:{port}"
            ...
    """

    def __init__(
        self,
        db_path: Path,
        port: int,
        *,
        log_dir: Optional[Path] = None,
        startup_timeout: int = 30,
    ) -> None:
        self.db_path = Path(db_path).resolve()
        self.port = port
        self.log_dir = (
            Path(log_dir).resolve() if log_dir else self.db_path.parent / "logs"
        )
        self.startup_timeout = startup_timeout
        self.proc: Optional[subprocess.Popen] = None

    @property
    def url(self) -> str:
        return f"http://127.0.0.1:{self.port}"

    @property
    def api(self) -> str:
        return f"{self.url}/api/v1"

    def start(self) -> None:
        env = os.environ.copy()
        env["DATABASE_URL"] = f"sqlite:///{self.db_path.as_posix()}"
        env["LOG_DIR"] = str(self.log_dir)
        # 噤声：测试不想要 Telegram poller / cleanup 干扰
        env["TELEGRAM_ENABLED"] = "false"
        env["TELEGRAM_SKIP_BACKLOG_ON_START"] = "true"
        env["PYTHONIOENCODING"] = "utf-8"
        # 阻止 .env 误覆盖（pydantic-settings 默认读 .env）
        env["DOTENV_PATH"] = ""  # 无效路径 → 视同无 .env

        self.db_path.parent.mkdir(parents=True, exist_ok=True)
        self.log_dir.mkdir(parents=True, exist_ok=True)

        py = str(SERVER_PYTHON) if SERVER_PYTHON.exists() else sys.executable
        self.proc = subprocess.Popen(
            [
                py,
                "-m",
                "uvicorn",
                "app.main:app",
                "--host",
                "127.0.0.1",
                "--port",
                str(self.port),
                "--log-level",
                "warning",
            ],
            cwd=str(SERVER_DIR),
            env=env,
            stdout=subprocess.DEVNULL,
            stderr=subprocess.PIPE,
            creationflags=subprocess.CREATE_NEW_PROCESS_GROUP
            if os.name == "nt"
            else 0,
        )

        # readiness probe
        deadline = time.monotonic() + self.startup_timeout
        last_err: Optional[Exception] = None
        while time.monotonic() < deadline:
            if self.proc.poll() is not None:
                err = (self.proc.stderr.read() or b"").decode(
                    "utf-8", errors="replace"
                )
                raise RuntimeError(
                    f"server exited prematurely (rc={self.proc.returncode}):\n{err}"
                )
            try:
                resp = requests.get(f"{self.url}/health", timeout=1)
                if resp.status_code == 200:
                    return
            except requests.RequestException as e:
                last_err = e
            time.sleep(0.2)
        self.stop()
        raise TimeoutError(f"server health check failed within {self.startup_timeout}s: {last_err}")

    def stop(self, *, hard: bool = False) -> None:
        if not self.proc:
            return
        if self.proc.poll() is not None:
            self.proc = None
            return
        try:
            if hard and os.name == "nt":
                # 模拟"服务端崩溃"：硬杀
                subprocess.run(
                    ["taskkill", "/PID", str(self.proc.pid), "/F", "/T"],
                    capture_output=True,
                )
            else:
                self.proc.terminate()
            try:
                self.proc.wait(timeout=5)
            except subprocess.TimeoutExpired:
                self.proc.kill()
                self.proc.wait(timeout=5)
        finally:
            self.proc = None

    def __enter__(self) -> "IsolatedServer":
        self.start()
        return self

    def __exit__(self, exc_type, exc, tb) -> None:
        self.stop()


# ---------------------------------------------------------------------------
# HTTP 辅助
# ---------------------------------------------------------------------------

def register_machine(
    api_base: str, machine_id: str, *, name: str = "", hostname: str = "INT"
) -> int:
    """向 /activity/report 发一次心跳，触发服务端自动注册员工；返回 employee_id。"""
    resp = requests.post(
        f"{api_base}/activity/report",
        json={
            "machine_id": machine_id,
            "name": name or None,
            "hostname": hostname,
            "idle_seconds": 0,
            "is_active": True,
        },
        timeout=10,
    )
    resp.raise_for_status()
    # 拿 employee_id：现成途径是查询员工列表（按 hostname 过滤太宽，按 name 不可靠）
    list_resp = requests.get(f"{api_base}/employees", timeout=10)
    list_resp.raise_for_status()
    for emp in list_resp.json():
        if emp.get("machine_id") == machine_id:
            return emp["id"]
    raise RuntimeError(f"employee not found after registration: {machine_id}")


def clock_in(api_base: str, employee_id: int) -> dict:
    return _post(f"{api_base}/debug/work/clock-in/{employee_id}")


def clock_out(api_base: str, employee_id: int) -> dict:
    return _post(f"{api_base}/debug/work/clock-out/{employee_id}")


def start_break(api_base: str, employee_id: int, break_type: str = "meal") -> dict:
    return _post(f"{api_base}/debug/work/break/{employee_id}/{break_type}")


def back_to_work(api_base: str, employee_id: int) -> dict:
    return _post(f"{api_base}/debug/work/back/{employee_id}")


def archive_employee(api_base: str, employee_id: int) -> dict:
    return _patch(f"{api_base}/employees/{employee_id}/archive")


def restore_employee(api_base: str, employee_id: int) -> dict:
    return _patch(f"{api_base}/employees/{employee_id}/restore")


def daily_stats(api_base: str, date_str: str, *, include_inactive: bool = False) -> list[dict]:
    resp = requests.get(
        f"{api_base}/windows/stats/daily",
        params={"date": date_str, "include_inactive": include_inactive},
        timeout=10,
    )
    resp.raise_for_status()
    return resp.json()


def employee_windows(
    api_base: str, employee_id: int, date_str: str, limit: int = 500
) -> list[dict]:
    resp = requests.get(
        f"{api_base}/windows/employees/{employee_id}",
        params={"date": date_str, "limit": limit},
        timeout=10,
    )
    resp.raise_for_status()
    return resp.json()


def _post(url: str) -> dict:
    resp = requests.post(url, timeout=10)
    resp.raise_for_status()
    return resp.json()


def _patch(url: str) -> dict:
    resp = requests.patch(url, timeout=10)
    resp.raise_for_status()
    return resp.json()


# ---------------------------------------------------------------------------
# DB 直读（绕过 ORM，只在断言/统计时用）
# ---------------------------------------------------------------------------

import sqlite3  # noqa: E402  保持 stdlib 顺序整洁


def db_query(db_path: Path, sql: str, params: tuple = ()) -> list[tuple]:
    """只读 SQL；WAL 模式下安全并发，不会阻塞 server。"""
    conn = sqlite3.connect(str(db_path), timeout=5)
    try:
        conn.execute("PRAGMA busy_timeout=5000")
        cur = conn.execute(sql, params)
        return cur.fetchall()
    finally:
        conn.close()


def count_raw(db_path: Path, employee_id: int) -> int:
    (n,) = db_query(
        db_path,
        "SELECT COUNT(*) FROM window_events_raw WHERE employee_id = ?",
        (employee_id,),
    )[0]
    return int(n)


def count_sessions(db_path: Path, employee_id: int) -> int:
    (n,) = db_query(
        db_path, "SELECT COUNT(*) FROM window_sessions WHERE employee_id = ?",
        (employee_id,),
    )[0]
    return int(n)


def count_raw_processed(db_path: Path, employee_id: int) -> int:
    (n,) = db_query(
        db_path,
        "SELECT COUNT(*) FROM window_events_raw "
        "WHERE employee_id = ? AND processed_at IS NOT NULL",
        (employee_id,),
    )[0]
    return int(n)


def fetch_sessions(db_path: Path, employee_id: int) -> list[dict]:
    rows = db_query(
        db_path,
        "SELECT id, work_state, process_name, normalized_title, "
        "started_at, ended_at, duration_seconds, source_event_count, shift_id "
        "FROM window_sessions WHERE employee_id = ? "
        "ORDER BY started_at",
        (employee_id,),
    )
    return [
        {
            "id": r[0], "work_state": r[1], "process_name": r[2],
            "normalized_title": r[3], "started_at": r[4],
            "ended_at": r[5], "duration_seconds": r[6],
            "source_event_count": r[7], "shift_id": r[8],
        }
        for r in rows
    ]

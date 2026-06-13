"""Step 5 End-to-End 联调：A–I + 压缩 J。

跑法（在 client/ 目录）：
    python tests/integration/verify_step5.py

策略：
- 隔离服务端（端口 9100，临时 SQLite）
- 每个场景独立 machine_id，互不污染
- 真实 WindowBuffer + 真实 WindowUploader + 真实 HTTP
- "tracker" 这一层用直接 buffer.append_event 等价仿真（避免 Win32 依赖）
- 全部用 ASCII [ok] / [FAIL] / [skip] 输出，方便 PS 控制台
"""
from __future__ import annotations

import os
import sys
import tempfile
import threading
import time
import uuid
from datetime import datetime, timedelta, timezone
from pathlib import Path

import requests

try:
    sys.stdout.reconfigure(encoding="utf-8")  # type: ignore[attr-defined]
    sys.stderr.reconfigure(encoding="utf-8")  # type: ignore[attr-defined]
except Exception:  # noqa: BLE001
    pass

sys.path.insert(0, str(Path(__file__).resolve().parents[2]))

from app.config import WindowTrackingConfig  # noqa: E402
from app.window_buffer import (  # noqa: E402
    STATUS_INVALID,
    STATUS_PENDING,
    STATUS_UPLOADED,
    WindowBuffer,
)
from app.window_uploader import WindowUploader  # noqa: E402
from tests.integration.harness import (  # noqa: E402
    IsolatedServer,
    archive_employee,
    back_to_work,
    clock_in,
    clock_out,
    count_raw,
    count_raw_processed,
    count_sessions,
    daily_stats,
    employee_windows,
    fetch_sessions,
    pick_free_port,
    register_machine,
    start_break,
)


# ---------------------------------------------------------------------------
# 报告聚合
# ---------------------------------------------------------------------------

REPORT: list[dict] = []
FAILURES: list[str] = []


def _section(name: str) -> None:
    print()
    print("=" * 60)
    print(name)
    print("=" * 60)


def _ok(name: str, detail: str = "") -> None:
    line = f"[ok] {name}" + (f" — {detail}" if detail else "")
    print(line)


def _record(scenario: str, ok: bool, detail: str) -> None:
    REPORT.append({"scenario": scenario, "ok": ok, "detail": detail})
    if not ok:
        FAILURES.append(f"{scenario}: {detail}")


def _fail(scenario: str, detail: str) -> None:
    print(f"[FAIL] {scenario}: {detail}")
    _record(scenario, False, detail)


def _assert(scenario: str, cond: bool, detail: str) -> bool:
    if cond:
        _ok(scenario, detail)
        _record(scenario, True, detail)
        return True
    _fail(scenario, detail)
    return False


def _iso(dt: datetime) -> str:
    return dt.astimezone(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def _today_utc() -> str:
    return datetime.now(timezone.utc).date().isoformat()


def _seed_event(buf: WindowBuffer, *, proc: str, title: str,
                started: datetime, ended: datetime,
                had_input: bool = True) -> str:
    eid = uuid.uuid4().hex
    buf.append_event(
        client_event_id=eid,
        process_name=proc,
        window_title=title,
        started_at=_iso(started),
        ended_at=_iso(ended),
        duration_seconds=max(1, int((ended - started).total_seconds())),
        had_input=had_input,
    )
    return eid


def _new_uploader(buf: WindowBuffer, machine_id: str, url: str,
                  *, batch_size: int = 500, max_attempts: int = 5,
                  session: requests.Session | None = None) -> WindowUploader:
    cfg = WindowTrackingConfig(
        upload_batch_size=batch_size,
        upload_interval_seconds=30,
        upload_backoff_seconds=[1, 2, 4, 8],
        upload_max_attempts=max_attempts,
    )
    return WindowUploader(
        cfg, buf,
        machine_id=machine_id,
        url=url,
        request_timeout_seconds=5,
        session=session,
    )


# ---------------------------------------------------------------------------
# 场景 A：正常上传
# ---------------------------------------------------------------------------

def scenario_A(srv: IsolatedServer, tmp: Path) -> None:
    _section("A · 正常上传：raw 写入 + processed_at 更新 + sessions 出现")
    mid = f"INT-A-{uuid.uuid4().hex[:6]}"
    emp = register_machine(srv.api, mid, name="A-Test")
    clock_in(srv.api, emp)

    buf = WindowBuffer(tmp / "A.db")
    base = datetime.now(timezone.utc) + timedelta(seconds=1)
    # 5 个事件：不同进程 + 同进程不同 title，覆盖 split / merge 路径
    plan = [
        ("chrome.exe", "Docs", 0, 30),
        ("chrome.exe", "Docs", 32, 60),  # 同 key + gap=2s ≤ merge_gap → 合并
        ("vscode.exe", "main.py", 65, 90),
        ("chrome.exe", "Docs", 95, 120),  # 同 key，gap=5s 边界 ← merge?
        ("notepad.exe", "Untitled", 130, 150),
    ]
    for proc, title, s, e in plan:
        _seed_event(buf, proc=proc, title=title,
                    started=base + timedelta(seconds=s),
                    ended=base + timedelta(seconds=e))

    up = _new_uploader(buf, mid, f"{srv.api}/windows/report")
    stats = up.drain_once()

    _assert("A.uploaded", stats["uploaded"] == 5,
            f"uploader 上传 5 / got={stats['uploaded']}")

    n_raw = count_raw(srv.db_path, emp)
    n_proc = count_raw_processed(srv.db_path, emp)
    n_sess = count_sessions(srv.db_path, emp)
    _assert("A.raw=5", n_raw == 5, f"window_events_raw 行数 / got={n_raw}")
    _assert("A.processed_at 全部更新", n_proc == 5, f"got={n_proc}")
    _assert("A.sessions 出现", n_sess >= 1, f"window_sessions 行数 / got={n_sess}")

    # buffer 中应全部 uploaded
    s = buf.stats()
    _assert("A.buffer.uploaded=5", s[STATUS_UPLOADED] == 5, f"got={s}")
    _assert("A.buffer.pending=0", s[STATUS_PENDING] == 0, f"got={s}")

    clock_out(srv.api, emp)


# ---------------------------------------------------------------------------
# 场景 B：process 切换 → 多 session 切分
# ---------------------------------------------------------------------------

def scenario_B(srv: IsolatedServer, tmp: Path) -> None:
    _section("B · 聚合正确性：Chrome → VSCode → Chrome 切分")
    mid = f"INT-B-{uuid.uuid4().hex[:6]}"
    emp = register_machine(srv.api, mid, name="B-Test")
    clock_in(srv.api, emp)
    time.sleep(0.5)
    base = datetime.now(timezone.utc) + timedelta(seconds=1)

    buf = WindowBuffer(tmp / "B.db")
    # 关键：第三段 chrome 与第一段 chrome 中间 gap=30s > merge_gap=5s
    # 即使 key 相同也**不应合并** → chrome 应为 2 个 session
    _seed_event(buf, proc="chrome.exe", title="A",
                started=base, ended=base + timedelta(seconds=30))
    _seed_event(buf, proc="vscode.exe", title="B",
                started=base + timedelta(seconds=30), ended=base + timedelta(seconds=60))
    _seed_event(buf, proc="chrome.exe", title="A",
                started=base + timedelta(seconds=60), ended=base + timedelta(seconds=90))

    up = _new_uploader(buf, mid, f"{srv.api}/windows/report")
    up.drain_once()

    sessions = fetch_sessions(srv.db_path, emp)
    _assert("B.sessions=3", len(sessions) == 3, f"got {len(sessions)} sessions")

    if len(sessions) == 3:
        procs = [s["process_name"] for s in sessions]
        _assert("B.顺序 chrome→vscode→chrome", procs == ["chrome.exe", "vscode.exe", "chrome.exe"],
                f"procs={procs}")
        durations = [s["duration_seconds"] for s in sessions]
        _assert("B.每段 30s", all(d == 30 for d in durations),
                f"durations={durations}")

    clock_out(srv.api, emp)


# ---------------------------------------------------------------------------
# 场景 C：break 边界切分（禁止 cross-break merge）
# ---------------------------------------------------------------------------

def scenario_C(srv: IsolatedServer, tmp: Path) -> None:
    _section("C · break 边界：working → break_meal → working 三段切分")
    mid = f"INT-C-{uuid.uuid4().hex[:6]}"
    emp = register_machine(srv.api, mid, name="C-Test")

    # 实时驱动 work_state 转换。重要：
    # - aggregator 用 int(duration_seconds)，亚秒段被丢，所以每段 ≥ 2s
    # - break > merge_gap=5s 才不会被跨 break 合并，所以 break = 7s
    clock_in(srv.api, emp)
    t0 = datetime.now(timezone.utc)
    time.sleep(3)          # working 段 1：3s
    start_break(srv.api, emp, "meal")
    time.sleep(7)          # break_meal 段：7s > merge_gap
    back_to_work(srv.api, emp)
    time.sleep(3)          # working 段 2：3s
    t3 = datetime.now(timezone.utc)

    # 事件起止比 shift_start / shift_end 各内推 1s，确保两端 working 段 ≥ 2s
    buf = WindowBuffer(tmp / "C.db")
    _seed_event(buf, proc="chrome.exe", title="Bench",
                started=t0 + timedelta(seconds=1),
                ended=t3 - timedelta(seconds=1))

    up = _new_uploader(buf, mid, f"{srv.api}/windows/report")
    up.drain_once()

    sessions = fetch_sessions(srv.db_path, emp)
    states = [s["work_state"] for s in sessions]
    _assert("C.sessions=3", len(sessions) == 3, f"got {len(sessions)} sessions states={states}")

    if len(sessions) == 3:
        _assert("C.状态序列 working→break_meal→working",
                states == ["working", "break_meal", "working"],
                f"states={states}")
        # 校验"禁止 cross-break merge"：前后两段 working 即使 key 相同也独立
        working = [s for s in sessions if s["work_state"] == "working"]
        _assert("C.两段 working 独立", len(working) == 2, f"got {len(working)}")
        _assert("C.两段 shift_id 一致", working[0]["shift_id"] == working[1]["shift_id"],
                f"shifts={[w['shift_id'] for w in working]}")

    clock_out(srv.api, emp)


# ---------------------------------------------------------------------------
# 场景 D：clock_out 边界
# ---------------------------------------------------------------------------

def scenario_D(srv: IsolatedServer, tmp: Path) -> None:
    _section("D · clock_out 边界：working → off_shift 拆段")
    mid = f"INT-D-{uuid.uuid4().hex[:6]}"
    emp = register_machine(srv.api, mid, name="D-Test")

    # 每段 ≥ 2s（aggregator 用 int() 落 duration，<1s 段被丢）
    clock_in(srv.api, emp)
    t0 = datetime.now(timezone.utc)
    time.sleep(3)          # working 段：3s
    clock_out(srv.api, emp)
    time.sleep(3)          # off_shift 段：3s
    t2 = datetime.now(timezone.utc)

    buf = WindowBuffer(tmp / "D.db")
    # 事件起止比 shift_start / shift_end 各内推 1s，确保 working / off_shift 段 ≥ 2s
    _seed_event(buf, proc="chrome.exe", title="Spanning",
                started=t0 + timedelta(seconds=1),
                ended=t2 - timedelta(seconds=1))

    up = _new_uploader(buf, mid, f"{srv.api}/windows/report")
    up.drain_once()

    sessions = fetch_sessions(srv.db_path, emp)
    states = [s["work_state"] for s in sessions]
    _assert("D.sessions>=2", len(sessions) >= 2, f"got {len(sessions)} states={states}")

    has_working = any(s["work_state"] == "working" for s in sessions)
    has_off = any(s["work_state"] == "off_shift" for s in sessions)
    _assert("D.含 working 段", has_working, f"states={states}")
    _assert("D.含 off_shift 段", has_off, f"states={states}")


# ---------------------------------------------------------------------------
# 场景 E：离线回放
# ---------------------------------------------------------------------------

def scenario_E(srv: IsolatedServer, tmp: Path) -> None:
    _section("E · 离线回放：网络断 → 数据保留 → 恢复后全部补传")
    mid = f"INT-E-{uuid.uuid4().hex[:6]}"
    emp = register_machine(srv.api, mid, name="E-Test")
    clock_in(srv.api, emp)
    time.sleep(0.5)
    base = datetime.now(timezone.utc) + timedelta(seconds=1)

    buf = WindowBuffer(tmp / "E.db")
    N = 30
    for i in range(N):
        _seed_event(buf, proc=f"proc{i % 5}.exe", title=f"T-{i}",
                    started=base + timedelta(seconds=i * 2),
                    ended=base + timedelta(seconds=i * 2 + 1))

    # 阶段 1：用一个失效 URL 模拟断网
    bad_up = _new_uploader(buf, mid,
                           "http://127.0.0.1:1/windows/report")  # 端口 1，连不上
    bad_stats = bad_up.drain_once()
    _assert("E.断网时 network_error", bad_stats["network_error"], f"got={bad_stats}")
    _assert("E.断网时 pending 保留", buf.stats()[STATUS_PENDING] == N,
            f"got pending={buf.stats()[STATUS_PENDING]}")
    _assert("E.断网时 服务端 raw=0", count_raw(srv.db_path, emp) == 0,
            f"got raw={count_raw(srv.db_path, emp)}")

    # 阶段 2：恢复"网络"
    good_up = _new_uploader(buf, mid, f"{srv.api}/windows/report")
    good_stats = good_up.drain_once()
    _assert("E.恢复后 uploaded=N", good_stats["uploaded"] == N,
            f"got={good_stats['uploaded']}")
    _assert("E.恢复后 buffer.pending=0",
            buf.stats()[STATUS_PENDING] == 0, "")
    _assert("E.恢复后 服务端 raw=N", count_raw(srv.db_path, emp) == N,
            f"got={count_raw(srv.db_path, emp)}")
    _assert("E.恢复后 processed_at=N",
            count_raw_processed(srv.db_path, emp) == N, "")

    clock_out(srv.api, emp)


# ---------------------------------------------------------------------------
# 场景 F：客户端"重启"（同 buffer 文件，新进程 / 新实例）
# ---------------------------------------------------------------------------

def scenario_F(srv: IsolatedServer, tmp: Path) -> None:
    _section("F · 客户端重启：buffer 文件持久；新进程实例续传")
    mid = f"INT-F-{uuid.uuid4().hex[:6]}"
    emp = register_machine(srv.api, mid, name="F-Test")
    clock_in(srv.api, emp)
    base = datetime.now(timezone.utc) + timedelta(seconds=1)

    buf_path = tmp / "F.db"

    # 阶段 1：进程 A 写 20 条然后"崩溃"（连接归零 = del 即可，WAL 已 commit）
    bufA = WindowBuffer(buf_path)
    for i in range(20):
        _seed_event(bufA, proc="chrome.exe", title=f"F-{i}",
                    started=base + timedelta(seconds=i * 2),
                    ended=base + timedelta(seconds=i * 2 + 1))
    del bufA  # 释放 WAL 句柄，模拟进程消失

    # 阶段 2：进程 B 打开同一 buffer，应当看到 20 条 pending
    bufB = WindowBuffer(buf_path)
    sB = bufB.stats()
    _assert("F.重启后 pending=20（WAL 持久化）",
            sB[STATUS_PENDING] == 20, f"got={sB}")

    up = _new_uploader(bufB, mid, f"{srv.api}/windows/report")
    stats = up.drain_once()
    _assert("F.续传 uploaded=20", stats["uploaded"] == 20, f"got={stats['uploaded']}")
    _assert("F.续传后 服务端 raw=20", count_raw(srv.db_path, emp) == 20,
            f"got={count_raw(srv.db_path, emp)}")

    clock_out(srv.api, emp)


# ---------------------------------------------------------------------------
# 场景 G：服务端重启
# ---------------------------------------------------------------------------

def scenario_G(tmp: Path) -> None:
    _section("G · 服务端重启：down → 累积 → up → 续传")
    db_path = tmp / "G_server.db"
    port = pick_free_port(9101)

    mid = f"INT-G-{uuid.uuid4().hex[:6]}"
    buf_path = tmp / "G_buf.db"
    buf = WindowBuffer(buf_path)

    # 阶段 0：起 server，注册并 clock_in
    srv = IsolatedServer(db_path, port)
    srv.start()
    try:
        emp = register_machine(srv.api, mid, name="G-Test")
        clock_in(srv.api, emp)
        time.sleep(0.3)
        base = datetime.now(timezone.utc) + timedelta(seconds=1)

        # 阶段 1：上传 5 条 OK
        for i in range(5):
            _seed_event(buf, proc="chrome.exe", title=f"G-pre-{i}",
                        started=base + timedelta(seconds=i),
                        ended=base + timedelta(seconds=i + 1))
        up1 = _new_uploader(buf, mid, f"{srv.api}/windows/report")
        s1 = up1.drain_once()
        _assert("G.阶段 1 uploaded=5", s1["uploaded"] == 5, f"got={s1}")

        # 阶段 2：服务端硬杀
        srv.stop(hard=True)

        # 阶段 3：继续产 10 条；尝试上传应 network_error
        for i in range(10):
            _seed_event(buf, proc="chrome.exe", title=f"G-mid-{i}",
                        started=base + timedelta(seconds=10 + i),
                        ended=base + timedelta(seconds=10 + i + 1))
        up2 = _new_uploader(buf, mid, f"{srv.api}/windows/report")
        s2 = up2.drain_once()
        _assert("G.停服 network_error", s2["network_error"], f"got={s2}")
        _assert("G.停服 pending=10", buf.stats()[STATUS_PENDING] == 10,
                f"got={buf.stats()}")

        # 阶段 4：起服 (同 DB)，续传
        srv2 = IsolatedServer(db_path, port)
        srv2.start()
        try:
            up3 = _new_uploader(buf, mid, f"{srv2.api}/windows/report")
            s3 = up3.drain_once()
            _assert("G.重启后 uploaded=10", s3["uploaded"] == 10, f"got={s3}")
            _assert("G.最终 server raw=15",
                    count_raw(db_path, emp) == 15, f"got={count_raw(db_path, emp)}")
        finally:
            srv2.stop()
    finally:
        srv.stop()


# ---------------------------------------------------------------------------
# 场景 H：client_event_id 幂等
# ---------------------------------------------------------------------------

def scenario_H(srv: IsolatedServer, tmp: Path) -> None:
    _section("H · 幂等：同 client_event_id 重复上传不产生重复 raw / session")
    mid = f"INT-H-{uuid.uuid4().hex[:6]}"
    emp = register_machine(srv.api, mid, name="H-Test")
    clock_in(srv.api, emp)
    time.sleep(0.5)
    base = datetime.now(timezone.utc) + timedelta(seconds=1)

    # 第一遍：buf1 → 5 条 → 上传 → 服务端 raw=5
    buf1 = WindowBuffer(tmp / "H1.db")
    ids = []
    for i in range(5):
        ids.append(_seed_event(buf1, proc="chrome.exe", title=f"H-{i}",
                               started=base + timedelta(seconds=i * 2),
                               ended=base + timedelta(seconds=i * 2 + 1)))
    up1 = _new_uploader(buf1, mid, f"{srv.api}/windows/report")
    up1.drain_once()
    raw1 = count_raw(srv.db_path, emp)
    sess1 = count_sessions(srv.db_path, emp)
    _assert("H.第 1 轮 raw=5", raw1 == 5, f"got={raw1}")

    # 第二遍：在新 buffer (buf2) 上以**同样** client_event_ids 再发一次
    buf2 = WindowBuffer(tmp / "H2.db")
    for i, eid in enumerate(ids):
        buf2.append_event(
            client_event_id=eid,  # ← 故意复用
            process_name="chrome.exe",
            window_title=f"H-{i}",
            started_at=_iso(base + timedelta(seconds=i * 2)),
            ended_at=_iso(base + timedelta(seconds=i * 2 + 1)),
            duration_seconds=1,
            had_input=True,
        )
    up2 = _new_uploader(buf2, mid, f"{srv.api}/windows/report")
    stats2 = up2.drain_once()
    _assert("H.第 2 轮 200 deduped=5（服务端 dedupe）",
            stats2["deduped"] == 5, f"got={stats2}")
    _assert("H.第 2 轮客户端仍 mark_uploaded", stats2["uploaded"] == 5,
            f"got={stats2}")

    raw2 = count_raw(srv.db_path, emp)
    sess2 = count_sessions(srv.db_path, emp)
    _assert("H.raw 仍 = 5（无重复）", raw2 == raw1, f"raw1={raw1} raw2={raw2}")
    _assert("H.sessions 不变", sess2 == sess1, f"sess1={sess1} sess2={sess2}")

    clock_out(srv.api, emp)


# ---------------------------------------------------------------------------
# 场景 I：archive employee
# ---------------------------------------------------------------------------

def scenario_I(srv: IsolatedServer, tmp: Path) -> None:
    _section("I · archive employee：record_report / windows report / 默认统计过滤")
    mid = f"INT-I-{uuid.uuid4().hex[:6]}"
    emp = register_machine(srv.api, mid, name="I-Test")
    clock_in(srv.api, emp)
    time.sleep(0.3)
    base = datetime.now(timezone.utc) + timedelta(seconds=1)

    # 在岗时上传 3 条
    buf = WindowBuffer(tmp / "I.db")
    for i in range(3):
        _seed_event(buf, proc="chrome.exe", title=f"I-pre-{i}",
                    started=base + timedelta(seconds=i),
                    ended=base + timedelta(seconds=i + 1))
    up = _new_uploader(buf, mid, f"{srv.api}/windows/report")
    up.drain_once()
    raw_pre = count_raw(srv.db_path, emp)
    _assert("I.archive 前 raw=3", raw_pre == 3, f"got={raw_pre}")

    # archive
    archive_employee(srv.api, emp)

    # archive 后 /activity/report 仍 200（数据照旧入库，是设计选择）
    resp = requests.post(
        f"{srv.api}/activity/report",
        json={"machine_id": mid, "name": None, "hostname": "INT",
              "idle_seconds": 0, "is_active": True},
        timeout=5,
    )
    _assert("I.archive 后 /activity/report 仍 200",
            resp.status_code == 200, f"got={resp.status_code}")

    # archive 后再上传几条窗口事件：路径文档说"仍 200 入库"
    base2 = datetime.now(timezone.utc) + timedelta(seconds=1)
    for i in range(2):
        _seed_event(buf, proc="vscode.exe", title=f"I-post-{i}",
                    started=base2 + timedelta(seconds=i),
                    ended=base2 + timedelta(seconds=i + 1))
    up2 = _new_uploader(buf, mid, f"{srv.api}/windows/report")
    s2 = up2.drain_once()
    _assert("I.archive 后 windows 上传仍成功（设计）", s2["uploaded"] == 2,
            f"got={s2}")
    raw_post = count_raw(srv.db_path, emp)
    _assert("I.archive 后 raw=5（入库未阻断）", raw_post == 5, f"got={raw_post}")

    # 默认统计应过滤掉 archived employee
    today = _today_utc()
    default_stats = daily_stats(srv.api, today)
    default_ids = {row["employee_id"] for row in default_stats}
    _assert("I.默认 daily_stats 过滤 archived", emp not in default_ids,
            f"emp={emp} default_ids={default_ids}")

    # include_inactive=true 能查到
    all_stats = daily_stats(srv.api, today, include_inactive=True)
    all_ids = {row["employee_id"] for row in all_stats}
    _assert("I.include_inactive=true 能查到", emp in all_ids,
            f"all_ids={all_ids}")

    # 历史端点（/windows/employees/{id}）无 is_active 过滤，应能看到
    history = employee_windows(srv.api, emp, today)
    _assert("I.历史端点仍可见 archived 数据", len(history) >= 1,
            f"history rows={len(history)}")


# ---------------------------------------------------------------------------
# 场景 J 压缩版：~3 分钟 + 高速喂数据 + 监控 uploader 资源
# ---------------------------------------------------------------------------

def scenario_J_compressed(srv: IsolatedServer, tmp: Path,
                          duration_seconds: int = 180) -> None:
    _section(f"J · 压缩 soak：{duration_seconds}s 真实 uploader + 高速 tracker 仿真")
    mid = f"INT-J-{uuid.uuid4().hex[:6]}"
    emp = register_machine(srv.api, mid, name="J-Soak")
    clock_in(srv.api, emp)

    buf = WindowBuffer(tmp / "J.db")
    # 真实 uploader 线程；upload_interval=1s 以加快节奏
    cfg = WindowTrackingConfig(
        upload_batch_size=200,
        upload_interval_seconds=1,
        upload_backoff_seconds=[1, 2, 4, 8],
        upload_max_attempts=5,
    )
    uploader = WindowUploader(
        cfg, buf,
        machine_id=mid,
        url=f"{srv.api}/windows/report",
        request_timeout_seconds=5,
    )

    import gc
    try:
        import psutil  # type: ignore
        proc = psutil.Process()
        have_psutil = True
    except ImportError:
        have_psutil = False
        proc = None

    samples: list[dict] = []
    stop_evt = threading.Event()

    def producer() -> None:
        i = 0
        base = datetime.now(timezone.utc)
        while not stop_evt.is_set():
            # 每 50ms 1 条：相当于 20 events/s（高于真实 0.5 events/s 40 倍）
            _seed_event(buf, proc=f"app{i % 7}.exe", title=f"T-{i % 50}",
                        started=base + timedelta(milliseconds=i * 50),
                        ended=base + timedelta(milliseconds=(i + 1) * 50))
            i += 1
            time.sleep(0.05)

    def sampler() -> None:
        while not stop_evt.is_set():
            row = {
                "ts": time.monotonic(),
                "buf_pending": buf.stats()[STATUS_PENDING],
                "buf_uploaded": buf.stats()[STATUS_UPLOADED],
            }
            if have_psutil:
                with proc.oneshot():
                    row["cpu_percent"] = proc.cpu_percent(interval=None)
                    row["rss_mb"] = proc.memory_info().rss / 1024 / 1024
                    row["threads"] = proc.num_threads()
            samples.append(row)
            time.sleep(2)

    uploader.start()
    if have_psutil:
        proc.cpu_percent(interval=None)  # 首次取 baseline，丢弃
    prod_th = threading.Thread(target=producer, daemon=True)
    samp_th = threading.Thread(target=sampler, daemon=True)
    prod_th.start()
    samp_th.start()

    t_start = time.monotonic()
    while time.monotonic() - t_start < duration_seconds:
        time.sleep(1)

    stop_evt.set()
    prod_th.join(timeout=5)
    uploader.stop()
    samp_th.join(timeout=5)
    gc.collect()

    n_raw = count_raw(srv.db_path, emp)
    s = buf.stats()
    print(f"   buffer final: pending={s[STATUS_PENDING]} uploaded={s[STATUS_UPLOADED]} invalid={s[STATUS_INVALID]}")
    print(f"   server raw: {n_raw}")
    if samples:
        if have_psutil:
            cpus = [r.get("cpu_percent", 0) for r in samples[1:]]  # 丢弃首拍 0
            rss = [r["rss_mb"] for r in samples]
            threads = [r["threads"] for r in samples]
            # 把样本切 4 段：前 25%（预热）/ 第二/三/四四分位
            n = len(samples)
            q1, q2, q3 = n // 4, n // 2, 3 * n // 4
            rss_quarters = [rss[:q1], rss[q1:q2], rss[q2:q3], rss[q3:]]
            quarter_growth = [
                (q[-1] - q[0]) if q else 0.0 for q in rss_quarters
            ]
            print(f"   cpu_percent: min={min(cpus):.1f} max={max(cpus):.1f} avg={sum(cpus)/max(1,len(cpus)):.1f}")
            print(f"   rss_mb full: min={min(rss):.1f} max={max(rss):.1f} delta={max(rss)-min(rss):.1f}")
            print("   rss_mb quarter growth (MB): Q1=%.1f Q2=%.1f Q3=%.1f Q4=%.1f" % tuple(quarter_growth))
            print(f"   threads full: min={min(threads)} max={max(threads)} "
                  f"steady (后半段): min={min(threads[q2:])} max={max(threads[q2:])}")
            # 真正的泄漏指标：第二半段（Q3+Q4）增量应低于第一半段（Q1+Q2）
            growth_first_half = quarter_growth[0] + quarter_growth[1]
            growth_second_half = quarter_growth[2] + quarter_growth[3]
            print(f"   first-half growth={growth_first_half:.1f}MB / second-half growth={growth_second_half:.1f}MB")
            _assert("J.无明显内存泄漏（后半段增量 <= 前半段增量 + 5MB 容差）",
                    growth_second_half <= growth_first_half + 5,
                    f"first={growth_first_half:.1f}MB second={growth_second_half:.1f}MB")
            # 总 RSS 增量软上限（180s 测试 < 50MB 视为可接受；扩展到 2h 时再看趋势）
            _assert("J.总 RSS 增量 < 50MB（180s 压缩 soak 容忍上限）",
                    (max(rss) - min(rss)) < 50,
                    f"delta={max(rss)-min(rss):.1f}MB")
            # 稳态后线程数不增长
            threads_steady = threads[q2:]
            _assert("J.稳态线程数稳定（max - min <= 2）",
                    (max(threads_steady) - min(threads_steady)) <= 2,
                    f"min={min(threads_steady)} max={max(threads_steady)}")
        pendings = [r["buf_pending"] for r in samples]
        print(f"   buf_pending: min={min(pendings)} max={max(pendings)} final={pendings[-1]}")
        # 上传速率应能跟上：终态 pending < N_total（最起码不溢出）
        _assert("J.uploader 追上 producer（终态 pending < 总产数）",
                pendings[-1] < s[STATUS_UPLOADED] + s[STATUS_PENDING],
                f"pending_final={pendings[-1]} total={s[STATUS_UPLOADED] + s[STATUS_PENDING]}")
    else:
        print("   [skip] psutil 未安装，仅监控 buffer")

    _assert("J.server raw 与 buffer uploaded 一致（无丢失）",
            n_raw == s[STATUS_UPLOADED], f"raw={n_raw} uploaded={s[STATUS_UPLOADED]}")

    clock_out(srv.api, emp)


# ---------------------------------------------------------------------------
# 主入口
# ---------------------------------------------------------------------------

def main(argv: list[str]) -> int:
    soak_seconds = 180  # 默认 3 分钟压缩 soak；用 --soak=600 可拉长
    skip_soak = False
    for a in argv[1:]:
        if a.startswith("--soak="):
            soak_seconds = int(a.split("=", 1)[1])
        elif a == "--no-soak":
            skip_soak = True

    overall_t = time.monotonic()
    with tempfile.TemporaryDirectory(ignore_cleanup_errors=True) as tmp:
        tmp_path = Path(tmp)
        db_path = tmp_path / "integration.db"
        port = pick_free_port(9100)

        with IsolatedServer(db_path, port) as srv:
            print(f"[harness] server url={srv.url} db={db_path}")
            try:
                scenario_A(srv, tmp_path)
                scenario_B(srv, tmp_path)
                scenario_C(srv, tmp_path)
                scenario_D(srv, tmp_path)
                scenario_E(srv, tmp_path)
                scenario_F(srv, tmp_path)
            except Exception as exc:  # noqa: BLE001
                _fail("A-F", f"unexpected exception: {exc!r}")
                import traceback
                traceback.print_exc()

        # G 自带 server 起停，独立 tmp dir
        try:
            scenario_G(tmp_path)
        except Exception as exc:  # noqa: BLE001
            _fail("G", f"unexpected exception: {exc!r}")
            import traceback
            traceback.print_exc()

        # H/I/J 共用第二台 server（确保 G 不污染）
        port2 = pick_free_port(9100)
        db2 = tmp_path / "hij.db"
        with IsolatedServer(db2, port2) as srv2:
            try:
                scenario_H(srv2, tmp_path)
                scenario_I(srv2, tmp_path)
                if not skip_soak:
                    scenario_J_compressed(srv2, tmp_path, soak_seconds)
                else:
                    print("[skip] J 压缩 soak 已通过 --no-soak 跳过")
            except Exception as exc:  # noqa: BLE001
                _fail("H-J", f"unexpected exception: {exc!r}")
                import traceback
                traceback.print_exc()

    # ----- 报告 -----
    elapsed = time.monotonic() - overall_t
    print()
    print("=" * 60)
    print(f"汇总：{len(REPORT)} 个断言，失败 {len(FAILURES)}，耗时 {elapsed:.1f}s")
    print("=" * 60)
    if FAILURES:
        print("失败明细：")
        for f in FAILURES:
            print(f"  - {f}")
        return 1
    print("ALL Step 5 integration scenarios PASSED")
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv))

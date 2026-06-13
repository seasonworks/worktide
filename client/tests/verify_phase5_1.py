"""Phase 5.1 · Startup / Single Instance 验证。

跑法（在 client/）::

    python tests/verify_phase5_1.py

仅 Windows 可用（``SingleInstance`` 用 Win32 ``CreateMutexW``，
非 Windows 平台跑这个脚本会被跳过返回 0）。

覆盖：
1. 首次 acquire 成功
2. 同进程内第二次 acquire 失败（占着锁的对象还活着）
3. 同进程 release 后再 acquire 成功
4. 跨进程：子进程持锁 → 父进程 acquire 失败（真证整机互斥，不只是同进程）
5. 子进程退出后父进程 acquire 成功（OS 兜底释放 mutex，没有死锁）

每条用例独立用一个 *专用* 测试 mutex 名（``Global\\EmployeeAgent_verify_phase5_1``），
**绝不**碰真实 agent 的 ``Global\\EmployeeAgent``，避免误伤正在跑的服务。
"""
from __future__ import annotations

import os
import subprocess
import sys
import textwrap
import time
from pathlib import Path

# 统一 stdout 编码为 utf-8，避免在中文 codepage 终端里日志乱码
try:
    sys.stdout.reconfigure(encoding="utf-8")  # type: ignore[attr-defined]
    sys.stderr.reconfigure(encoding="utf-8")  # type: ignore[attr-defined]
except Exception:  # noqa: BLE001
    pass

# 让 tests/ 可以 import app/
sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

if sys.platform != "win32":
    print("[SKIP] verify_phase5_1.py: Windows-only (SingleInstance uses Win32 CreateMutex)")
    sys.exit(0)

from app.single_instance import SingleInstance  # noqa: E402

# 专用测试名：与真实 mutex Global\EmployeeAgent 完全不同，跑测试时不会把正在
# 运行的 agent 锁死或挤掉。
TEST_NAME = "Global\\EmployeeAgent_verify_phase5_1"

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


# ─────────────────────────────────────────────────────────────────────────────
# 1) 同进程：第一次 acquire 拿到
# ─────────────────────────────────────────────────────────────────────────────
def t1_first_acquire_succeeds() -> None:
    lock = SingleInstance(TEST_NAME)
    try:
        assert lock.acquire(), "first acquire should succeed"
    finally:
        lock.release()


# ─────────────────────────────────────────────────────────────────────────────
# 2) 同进程：第二个对象 acquire 失败
# ─────────────────────────────────────────────────────────────────────────────
def t2_second_acquire_fails() -> None:
    a = SingleInstance(TEST_NAME)
    b = SingleInstance(TEST_NAME)
    try:
        assert a.acquire(), "first acquire should succeed"
        assert not b.acquire(), "second acquire should fail while first is held"
    finally:
        b.release()
        a.release()


# ─────────────────────────────────────────────────────────────────────────────
# 3) release 后可再 acquire
# ─────────────────────────────────────────────────────────────────────────────
def t3_release_allows_reacquire() -> None:
    a = SingleInstance(TEST_NAME)
    assert a.acquire()
    a.release()
    b = SingleInstance(TEST_NAME)
    try:
        assert b.acquire(), "should reacquire after release"
    finally:
        b.release()


# ─────────────────────────────────────────────────────────────────────────────
# 4) 跨进程：子进程持锁 → 父进程 acquire 失败
# ─────────────────────────────────────────────────────────────────────────────
_CHILD_HOLDER = textwrap.dedent(
    """
    import sys, time
    from pathlib import Path
    sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
    from app.single_instance import SingleInstance
    lock = SingleInstance({name!r})
    ok = lock.acquire()
    # 让父进程能从 stdout 上区分"已持锁"和"启动出错"
    print("CHILD_ACQUIRED" if ok else "CHILD_FAILED", flush=True)
    # 持锁等待，父进程在 5 秒内做完检测就 kill 我
    time.sleep(10)
    """
).strip()


def t4_cross_process_blocks_parent() -> None:
    child_path = Path(__file__).with_name("_phase5_1_holder.py")
    child_path.write_text(_CHILD_HOLDER.format(name=TEST_NAME), encoding="utf-8")
    proc = subprocess.Popen(
        [sys.executable, str(child_path)],
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        text=True,
        encoding="utf-8",
        bufsize=1,
    )
    try:
        # 等子进程报告拿到锁；阻塞 readline 即可，最多 5s
        deadline = time.time() + 5
        line = ""
        while time.time() < deadline:
            if proc.stdout is None:
                break
            line = proc.stdout.readline().strip()
            if line:
                break
        assert line == "CHILD_ACQUIRED", f"expected CHILD_ACQUIRED, got {line!r}"

        # 父进程同名 acquire 必须失败
        parent_lock = SingleInstance(TEST_NAME)
        try:
            assert not parent_lock.acquire(), (
                "parent acquire should fail while child holds the mutex"
            )
        finally:
            parent_lock.release()
    finally:
        proc.kill()
        proc.wait(timeout=5)
        try:
            child_path.unlink()
        except OSError:
            pass


# ─────────────────────────────────────────────────────────────────────────────
# 5) 子进程退出后父进程可 acquire（OS 自动释放 mutex，没死锁）
# ─────────────────────────────────────────────────────────────────────────────
_CHILD_QUICK = textwrap.dedent(
    """
    import sys
    from pathlib import Path
    sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
    from app.single_instance import SingleInstance
    lock = SingleInstance({name!r})
    lock.acquire()
    # 立即退出，**不** release —— 验证 OS 兜底
    """
).strip()


def t5_child_exit_releases_mutex() -> None:
    child_path = Path(__file__).with_name("_phase5_1_quick.py")
    child_path.write_text(_CHILD_QUICK.format(name=TEST_NAME), encoding="utf-8")
    try:
        rc = subprocess.call([sys.executable, str(child_path)], timeout=10)
        assert rc == 0, f"child exited with rc={rc}"
        # 子进程已退出，OS 应已释放 mutex
        parent_lock = SingleInstance(TEST_NAME)
        try:
            assert parent_lock.acquire(), (
                "parent should acquire after child process exit (OS cleanup)"
            )
        finally:
            parent_lock.release()
    finally:
        try:
            child_path.unlink()
        except OSError:
            pass


# ─────────────────────────────────────────────────────────────────────────────
# 入口
# ─────────────────────────────────────────────────────────────────────────────
case("1. first acquire succeeds", t1_first_acquire_succeeds)
case("2. second acquire fails while first is held", t2_second_acquire_fails)
case("3. release allows reacquire", t3_release_allows_reacquire)
case("4. cross-process: parent blocked while child holds", t4_cross_process_blocks_parent)
case("5. child exit releases mutex (OS cleanup)", t5_child_exit_releases_mutex)

print()
print(f"{_pass} passed, {_fail} failed")
sys.exit(0 if _fail == 0 else 1)

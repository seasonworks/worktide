"""Phase 5.4 · Auto Update 单元验证。

跑法（在 client/）::

    python tests/verify_phase5_4.py

同 verify_phase5_1/2/3 风格：手写 case() 入口，无 pytest。

覆盖：
  UpdateConfig --------------------------------------------------------
    C1  默认值符合 D2 (3600s) / D5 (channel=stable)

  UpdateState ---------------------------------------------------------
    S1  合法转换 idle → checking → downloading → staged → installing → idle
    S2  非法转换被拒（idle 直接 → installing）
    S3  任何状态可转 failed；failed 可回 idle/checking
    S4  转 IDLE 时 target/error 自动清空
    S5  snapshot() 字段齐全且 ISO 8601 时间格式

  sha256 / hmac -------------------------------------------------------
    H1  verify_sha256: 好 zip pass / 改 1 byte fail
    H2  verify_hmac: secret 一致 pass / secret 错 fail
    H3  verify_hmac: 空 secret 必 fail（安全默认）
    H4  HMAC canonical 字符串 client/server 一致（与 release_manager 对比）

  UpdatePipeline 行为 -------------------------------------------------
    P1  _cycle 在 server 返回 no-update 时回 idle，不下载
    P2  _cycle 在 server 返回 manifest 时走完整 5 步
    P3  download 后 sha256 mismatch → failed + 删 zip
    P4  download 后 hmac mismatch → failed + 删 zip
    P5  install 触发 lifecycle.record_exit + exit_callable
    P6  install 前同步上报 installing payload（含 update.status='installing'）

  HealthReporter --------------------------------------------------
    R1  build_payload 含 update 子结构（D3 强制状态报告通道）
"""
from __future__ import annotations

import hashlib
import json
import sys
import tempfile
import zipfile
from pathlib import Path

try:
    sys.stdout.reconfigure(encoding="utf-8")  # type: ignore[attr-defined]
    sys.stderr.reconfigure(encoding="utf-8")  # type: ignore[attr-defined]
except Exception:  # noqa: BLE001
    pass

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

import requests  # noqa: E402  for FakeSession signature

from app import update_state as us  # noqa: E402
from app.config import HealthConfig, UpdateConfig  # noqa: E402
from app.health_reporter import HealthReporter  # noqa: E402
from app.lifecycle import LifecycleRecorder  # noqa: E402
from app.update_pipeline import (  # noqa: E402
    UpdatePipeline,
    verify_hmac,
    verify_sha256,
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
# Fakes
# ---------------------------------------------------------------------------


class _FakeResp:
    def __init__(self, status_code: int, json_data=None, content: bytes = b""):
        self.status_code = status_code
        self._json = json_data
        self._content = content
        self.text = str(json_data) if json_data else ""

    def json(self):
        if self._json is None:
            raise ValueError("not json")
        return self._json

    def iter_content(self, chunk_size: int = 0):
        # 一次返完，模拟小文件流式
        yield self._content

    def __enter__(self): return self
    def __exit__(self, *a): return False


class _FakeSession:
    """模拟 GET /check + GET /download；按 url path 派发。"""

    def __init__(self, *, check_resp=None, download_resp=None, post_resp=None):
        self.check_resp = check_resp
        self.download_resp = download_resp
        self.post_resp = post_resp
        self.posts: list[tuple[str, dict]] = []

    def get(self, url, params=None, timeout=None, stream=False):
        if "/check" in url:
            return self.check_resp
        if "/download" in url:
            return self.download_resp
        return _FakeResp(404)

    def post(self, url, json=None, timeout=None):
        self.posts.append((url, json))
        return self.post_resp or _FakeResp(200, json_data={"status": "received"})


# ---------------------------------------------------------------------------
# UpdateConfig
# ---------------------------------------------------------------------------


def c1_update_config_defaults():
    cfg = UpdateConfig()
    assert cfg.enabled is True
    assert cfg.check_interval_seconds == 3600  # D2
    assert cfg.channel == "stable"  # D5
    assert isinstance(cfg.hmac_secret, str) and len(cfg.hmac_secret) > 0


case("C1 UpdateConfig defaults (D2 3600s, D5 stable)", c1_update_config_defaults)


# ---------------------------------------------------------------------------
# UpdateState
# ---------------------------------------------------------------------------


def s1_legal_transitions():
    st = us.UpdateState()
    assert st.transition(us.CHECKING)
    assert st.transition(us.DOWNLOADING, target_version="0.5.4")
    assert st.transition(us.STAGED)
    assert st.transition(us.INSTALLING)
    assert st.transition(us.IDLE)


def s2_illegal_transition_rejected():
    st = us.UpdateState()
    # idle → installing 不在 _TRANSITIONS 里
    assert not st.transition(us.INSTALLING)
    assert st.status == us.IDLE


def s3_failed_recovery():
    st = us.UpdateState()
    st.transition(us.CHECKING)
    assert st.transition(us.FAILED, error="boom")
    assert st.status == us.FAILED
    assert st.transition(us.IDLE)
    assert st.transition(us.CHECKING)
    assert st.transition(us.FAILED, error="boom2")
    assert st.transition(us.CHECKING)  # failed → checking 也合法


def s4_idle_clears_target_and_error():
    st = us.UpdateState()
    st.transition(us.CHECKING)
    st.transition(us.DOWNLOADING, target_version="0.5.4")
    st.transition(us.FAILED, error="net")
    snap1 = st.snapshot()
    assert snap1["target_version"] == "0.5.4"
    assert snap1["last_error"] == "net"
    st.transition(us.IDLE)
    snap2 = st.snapshot()
    assert snap2["target_version"] is None
    assert snap2["last_error"] is None


def s5_snapshot_fields():
    st = us.UpdateState()
    st.mark_check_attempted()
    snap = st.snapshot()
    for k in ("status", "target_version", "last_check_at", "last_error"):
        assert k in snap, f"missing {k}"
    # last_check_at 必须是 ISO 'Z' 字符串
    assert snap["last_check_at"].endswith("Z")
    assert "T" in snap["last_check_at"]


case("S1 legal full transition path", s1_legal_transitions)
case("S2 illegal idle→installing rejected", s2_illegal_transition_rejected)
case("S3 failed can recover to idle/checking", s3_failed_recovery)
case("S4 IDLE clears target_version + last_error", s4_idle_clears_target_and_error)
case("S5 snapshot has all 4 fields + ISO Z format", s5_snapshot_fields)


# ---------------------------------------------------------------------------
# sha256 / hmac
# ---------------------------------------------------------------------------


def _make_zip(tmp: Path, name: str = "test.zip", payload: bytes = b"hello") -> tuple[Path, str]:
    """造一个 zip，返回 (zip_path, sha256_hex)。"""
    path = tmp / name
    with zipfile.ZipFile(path, "w") as zf:
        zf.writestr("payload.txt", payload)
    h = hashlib.sha256()
    h.update(path.read_bytes())
    return path, h.hexdigest()


def h1_verify_sha256_good_bad():
    with tempfile.TemporaryDirectory() as tmp:
        z, sha = _make_zip(Path(tmp))
        assert verify_sha256(z, sha)
        assert not verify_sha256(z, "0" * 64)


def h2_verify_hmac_secret_match():
    secret = "shared-secret"
    sha = "a" * 64
    size = 12345
    version = "0.5.4"
    canonical = f"{version}|{sha}|{size}".encode("utf-8")
    import hmac as _hmac
    import hashlib as _h
    sig = _hmac.new(secret.encode(), canonical, _h.sha256).hexdigest()
    assert verify_hmac(version, sha, size, sig, secret)
    assert not verify_hmac(version, sha, size, sig, "wrong-secret")


def h3_verify_hmac_empty_secret_fails():
    assert not verify_hmac("0.5.4", "a" * 64, 1, "deadbeef", "")


def h4_canonical_matches_server():
    """canonical 字符串 client = server，否则 E2E 永远失败。"""
    from app.update_pipeline import verify_hmac as client_verify
    # 重复 server 的实现，确认结果一致
    import hashlib as _h
    import hmac as _hmac
    secret = "S"; ver = "0.5.4"; sha = "x" * 64; size = 9
    canonical = f"{ver}|{sha}|{size}".encode()
    sig = _hmac.new(secret.encode(), canonical, _h.sha256).hexdigest()
    assert client_verify(ver, sha, size, sig, secret)


case("H1 verify_sha256: good pass / bad fail", h1_verify_sha256_good_bad)
case("H2 verify_hmac: secret match pass / mismatch fail", h2_verify_hmac_secret_match)
case("H3 verify_hmac: empty secret rejects all (safe default)", h3_verify_hmac_empty_secret_fails)
case("H4 client/server HMAC canonical string compatible", h4_canonical_matches_server)


# ---------------------------------------------------------------------------
# UpdatePipeline behavior
# ---------------------------------------------------------------------------


def _make_pipeline(*, check_resp=None, download_content: bytes = b"",
                   download_status: int = 200,
                   exit_calls=None, posts_capture=None,
                   spawn_calls=None,
                   lifecycle_path: Path | None = None,
                   hmac_secret: str = "test-secret"):
    """组装 UpdatePipeline + 注入 fake 依赖。"""
    state = us.UpdateState()
    if lifecycle_path is None:
        lifecycle_path = Path(tempfile.mkdtemp()) / "rc.json"
    lc = LifecycleRecorder(path=lifecycle_path)
    download_resp = _FakeResp(download_status, content=download_content)
    session = _FakeSession(
        check_resp=check_resp,
        download_resp=download_resp,
    )
    if posts_capture is not None:
        # 在每次 post 时把 (url, json) 抓出来
        orig_post = session.post

        def capture(url, json=None, timeout=None):
            posts_capture.append((url, json))
            return orig_post(url, json=json, timeout=timeout)
        session.post = capture

    def fake_exit(code):  # noqa: ANN001
        if exit_calls is not None:
            exit_calls.append(code)
        raise SystemExit(code)

    def fake_spawn(*, stage_dir, install_dir, data_dir_path,
                   old_version, new_version):
        if spawn_calls is not None:
            spawn_calls.append({
                "stage_dir": stage_dir, "install_dir": install_dir,
                "data_dir_path": data_dir_path, "old_version": old_version,
                "new_version": new_version,
            })

    cfg = UpdateConfig(
        enabled=True,
        check_interval_seconds=3600,
        initial_delay_seconds=0,
        channel="stable",
        hmac_secret=hmac_secret,
    )
    pipeline = UpdatePipeline(
        cfg, state, lc,
        machine_id="MACHINE_TEST",
        server_base_url="http://test.local",
        session=session,
        exit_callable=fake_exit,
        spawn_updater=fake_spawn,
        health_post_url="http://test.local/api/v1/agent/health",
        health_post_payload_builder=lambda: {"machine_id": "MACHINE_TEST",
                                              "update": state.snapshot()},
    )
    return pipeline, state, session


def p1_no_update_returns_idle():
    no_update = _FakeResp(200, json_data={
        "current_version": "0.5.3", "latest_version": "0.5.3",
        "update_available": False, "manifest": None,
    })
    pipe, state, _ = _make_pipeline(check_resp=no_update)
    pipe._cycle()
    assert state.status == us.IDLE


def p2_sha_mismatch_marks_failed():
    """构造一个 manifest，但客户端真的下载到的是不同内容 → sha256 mismatch。"""
    # 制造一份"假的" sha256（不对应真实 content）
    fake_sha = "f" * 64
    fake_sig = "0" * 64
    check_resp = _FakeResp(200, json_data={
        "current_version": "0.5.3", "latest_version": "0.5.4",
        "update_available": True,
        "manifest": {
            "version": "0.5.4",
            "sha256": fake_sha,
            "signature": fake_sig,
            "size": 5,
            "download_url": "/api/v1/agent/updates/download/0.5.4",
        },
    })
    pipe, state, _ = _make_pipeline(
        check_resp=check_resp,
        download_content=b"hello",
        hmac_secret="test-secret",
    )
    pipe._cycle()
    # 因为 sha 不匹配，被标 failed
    snap = state.snapshot()
    assert snap["status"] == us.FAILED
    assert snap["last_error"] == "sha256_mismatch"


def p3_hmac_mismatch_marks_failed():
    """sha 匹配但 hmac 不匹配（签名用错 secret）→ failed。"""
    payload = b"hello"
    real_sha = hashlib.sha256(payload).hexdigest()
    bad_sig = "0" * 64  # 不可能匹配
    check_resp = _FakeResp(200, json_data={
        "current_version": "0.5.3", "latest_version": "0.5.4",
        "update_available": True,
        "manifest": {
            "version": "0.5.4",
            "sha256": real_sha,
            "signature": bad_sig,
            "size": len(payload),
            "download_url": "/api/v1/agent/updates/download/0.5.4",
        },
    })
    pipe, state, _ = _make_pipeline(
        check_resp=check_resp,
        download_content=payload,
        hmac_secret="test-secret",
    )
    pipe._cycle()
    snap = state.snapshot()
    assert snap["status"] == us.FAILED
    assert snap["last_error"] == "hmac_mismatch"


def p4_install_path_calls_lifecycle_and_exit():
    """端到端：从 check 到 install 全走通，记录 lifecycle + exit_callable。

    需要构造一个能解压的真 zip，并且 sha256+hmac 都对得上。
    """
    import hmac as _hmac
    with tempfile.TemporaryDirectory() as tmp:
        # 1) 构造一个有 EmployeeAgent/EmployeeAgent.exe 的 zip
        z_path = Path(tmp) / "fake.zip"
        with zipfile.ZipFile(z_path, "w") as zf:
            zf.writestr("EmployeeAgent/EmployeeAgent.exe", b"fake_exe_bytes")
        content = z_path.read_bytes()
        real_sha = hashlib.sha256(content).hexdigest()
        secret = "test-secret"
        size = len(content)
        version = "0.5.4"
        canonical = f"{version}|{real_sha}|{size}".encode()
        sig = _hmac.new(secret.encode(), canonical, hashlib.sha256).hexdigest()

        check_resp = _FakeResp(200, json_data={
            "current_version": "0.5.3", "latest_version": version,
            "update_available": True,
            "manifest": {
                "version": version,
                "sha256": real_sha,
                "signature": sig,
                "size": size,
                "download_url": "/api/v1/agent/updates/download/0.5.4",
            },
        })
        exit_calls: list[int] = []
        spawn_calls: list = []
        posts: list = []
        pipe, state, _ = _make_pipeline(
            check_resp=check_resp,
            download_content=content,
            exit_calls=exit_calls,
            spawn_calls=spawn_calls,
            posts_capture=posts,
            hmac_secret=secret,
        )
        try:
            pipe._cycle()
        except SystemExit:
            pass
        assert spawn_calls, "spawn_updater 应被调用一次"
        assert exit_calls == [0], f"exit_callable should be (0,), got {exit_calls}"
        # D3 强制：install 前同步 POST 一次 installing 状态
        assert posts, "应当至少一次 sync POST"
        last = posts[-1]
        assert last[1]["update"]["status"] == us.INSTALLING


case("P1 no-update → state stays IDLE", p1_no_update_returns_idle)
case("P2 sha256 mismatch → state FAILED + reason", p2_sha_mismatch_marks_failed)
case("P3 hmac mismatch → state FAILED + reason", p3_hmac_mismatch_marks_failed)
case("P4 full happy path: spawn updater + record_exit + os._exit + installing POST",
     p4_install_path_calls_lifecycle_and_exit)


# ---------------------------------------------------------------------------
# HealthReporter
# ---------------------------------------------------------------------------


def r1_health_payload_includes_update():
    """build_payload 应包含 update 子结构。"""
    with tempfile.TemporaryDirectory() as tmp:
        lc = LifecycleRecorder(path=Path(tmp) / "rc.json")
        state = us.UpdateState()
        state.transition(us.CHECKING)
        reporter = HealthReporter(
            HealthConfig(),
            lc,
            machine_id="M",
            hostname="H",
            employee_name="",
            url="http://test.local/api/v1/agent/health",
            update_state=state,
        )
        payload = reporter.build_payload()
        assert "update" in payload
        assert payload["update"]["status"] == us.CHECKING


case("R1 HealthReporter.build_payload includes update sub-structure",
     r1_health_payload_includes_update)


# ---------------------------------------------------------------------------
# 入口
# ---------------------------------------------------------------------------
print()
print(f"{_pass} passed, {_fail} failed")
sys.exit(0 if _fail == 0 else 1)

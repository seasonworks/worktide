"""#4 · 后台鉴权冒烟（token / 密码 / 节流 / 白名单 + 中间件集成）。

    server/.venv/Scripts/python.exe server/scripts/smoke_auth.py

覆盖：
    单元
      1  verify_password 对/错
      2  create_token→verify_token；篡改/换密钥/过期 → 拒
      3  is_open_path：agent 端点放行、后台端点不放行、OPTIONS/health/login 放行
      4  登录节流：连续失败到阈值 → 锁；reset 解锁
    集成（TestClient，不跑 lifespan→不起线程/poller）
      5  无 token 访问后台端点 → 401
      6  OPTIONS 预检 → 非 401（放行）
      7  /health → 200
      8  登录：错密码 401、未配置 503（临时清配置）、对密码 200 且 token 可验
      9  带 token 访问后台端点 → 非 401（鉴权放行）
     10  agent 端点（POST activity/report）无 token → 非 401（保持开放）
"""
from __future__ import annotations

import os
import sys
from pathlib import Path

if hasattr(sys.stdout, "reconfigure"):
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")

os.environ.setdefault("DATABASE_URL", "sqlite:///:memory:")
os.environ.setdefault("CORS_ORIGINS", "[]")
os.environ.setdefault("TELEGRAM_ENABLED", "false")
os.environ.setdefault("TELEGRAM_BOT_TOKEN", "dummy:test")
os.environ.setdefault("TELEGRAM_CHAT_ID", "-100123")

_HERE = Path(__file__).resolve()
_SERVER = _HERE.parents[1]
if str(_SERVER) not in sys.path:
    sys.path.insert(0, str(_SERVER))

import time  # noqa: E402

from app import auth  # noqa: E402
from app.config import settings  # noqa: E402

PASS = 0
FAIL: list[str] = []


def ok(label: str, cond: bool, detail: str = "") -> None:
    global PASS
    if cond:
        PASS += 1
        print(f"  ✅ {label}")
    else:
        FAIL.append(label)
        print(f"  ❌ {label}   {detail}")


# 配置后台鉴权（运行期注入，不依赖真 env）
_P = settings.api_v1_prefix
settings.admin_session_secret = "smoke-session-secret-32bytes-xxxxxxxx"
settings.admin_password_hash = auth.hash_password("correct-horse-battery")

print("[1] 密码校验")
ok("对密码 → True", auth.verify_password("correct-horse-battery"))
ok("错密码 → False", not auth.verify_password("wrong"))
ok("空哈希配置 → False", not (lambda: (
    setattr(settings, "admin_password_hash", "") or auth.verify_password("x")
))())
settings.admin_password_hash = auth.hash_password("correct-horse-battery")  # 还原

print("[2] token 签发/校验")
tok = auth.create_token()
ok("自签 token 可验", auth.verify_token(tok))
ok("篡改签名 → 拒", not auth.verify_token(tok[:-3] + ("aaa" if not tok.endswith("aaa") else "bbb")))
ok("乱码 token → 拒", not auth.verify_token("garbage"))
ok("None → 拒", not auth.verify_token(None))
expired = auth.create_token(ttl_seconds=-10)
ok("过期 token → 拒", not auth.verify_token(expired))
# 换密钥后旧 token 失效
_old = settings.admin_session_secret
settings.admin_session_secret = "different-secret-zzzzzzzzzzzzzzzzzzzz"
ok("换签名密钥后旧 token → 拒", not auth.verify_token(tok))
settings.admin_session_secret = _old

print("[3] is_open_path 白名单")
ok("OPTIONS 预检放行", auth.is_open_path("OPTIONS", f"{_P}/employees"))
ok("/health 放行", auth.is_open_path("GET", "/health"))
ok("/auth/login 放行", auth.is_open_path("POST", f"{_P}/auth/login"))
ok("agent activity/report 放行", auth.is_open_path("POST", f"{_P}/activity/report"))
ok("agent windows/report 放行", auth.is_open_path("POST", f"{_P}/windows/report"))
ok("agent /agent/health 放行", auth.is_open_path("POST", f"{_P}/agent/health"))
ok("agent /agent/updates/check 放行", auth.is_open_path("GET", f"{_P}/agent/updates/check"))
ok("后台 employees 不放行", not auth.is_open_path("GET", f"{_P}/employees"))
ok("后台 settings 不放行", not auth.is_open_path("PATCH", f"{_P}/settings/x"))
ok("后台 activity/recent 不放行", not auth.is_open_path("GET", f"{_P}/activity/recent"))
ok("非业务前缀 /docs 放行", auth.is_open_path("GET", "/docs"))

print("[4] 登录节流")
auth.reset_fails("1.2.3.4")
ok("初始未锁", auth.throttle_retry_after("1.2.3.4") == 0)
for _ in range(5):
    auth.record_fail("1.2.3.4")
ok("连续失败到阈值 → 锁", auth.throttle_retry_after("1.2.3.4") > 0)
auth.reset_fails("1.2.3.4")
ok("reset 后解锁", auth.throttle_retry_after("1.2.3.4") == 0)

print("[5-10] 中间件集成（TestClient，不跑 lifespan）")
from fastapi.testclient import TestClient  # noqa: E402
import app.main as main_mod  # noqa: E402

# 不用 with → 不触发 lifespan（不起线程/poller）；raise_server_exceptions=False 让
# 内存库无表导致的 500 作为响应返回而非抛出——本套只验鉴权层（非 401 即放行通过）。
client = TestClient(main_mod.app, raise_server_exceptions=False)

# [5] 无 token 后台端点 → 401
r = client.get(f"{_P}/activity/recent")
ok("无 token 后台端点 → 401", r.status_code == 401, str(r.status_code))

# [6] OPTIONS 预检 → 非 401
r = client.options(f"{_P}/activity/recent", headers={
    "Origin": "http://localhost:5173",
    "Access-Control-Request-Method": "GET",
})
ok("OPTIONS 预检 → 非 401", r.status_code != 401, str(r.status_code))

# [7] /health → 200
r = client.get("/health")
ok("/health → 200", r.status_code == 200, str(r.status_code))

# [8] 登录流程
r = client.post(f"{_P}/auth/login", json={"password": "nope"})
ok("错密码 → 401", r.status_code == 401, str(r.status_code))
# 未配置 → 503
_saved = settings.admin_password_hash
settings.admin_password_hash = ""
r = client.post(f"{_P}/auth/login", json={"password": "x"})
ok("未配置 → 503", r.status_code == 503, str(r.status_code))
settings.admin_password_hash = _saved
r = client.post(f"{_P}/auth/login", json={"password": "correct-horse-battery"})
ok("对密码 → 200", r.status_code == 200, str(r.status_code))
issued = r.json().get("token", "") if r.status_code == 200 else ""
ok("签发的 token 可验", auth.verify_token(issued))

# [9] 带 token 访问后台端点 → 非 401（鉴权放行；后续 200/500 视 DB 而定）
r = client.get(f"{_P}/activity/recent", headers={"Authorization": f"Bearer {issued}"})
ok("带 token 后台端点 → 非 401", r.status_code != 401, str(r.status_code))

# [10] agent 端点无 token → 非 401（保持开放）
r = client.post(f"{_P}/activity/report", json={
    "machine_id": "m_smoke", "hostname": "h_smoke", "idle_seconds": 10,
})
ok("agent activity/report 无 token → 非 401", r.status_code != 401, str(r.status_code))

print()
print("=" * 60)
print(f"PASS={PASS}  FAIL={len(FAIL)}")
if FAIL:
    for label in FAIL:
        print(f"  - {label}")
    sys.exit(1)
print("后台鉴权全部通过（token/密码/节流/白名单/中间件）")

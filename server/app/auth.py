"""#4 · 应用层后台鉴权（标准库实现，无第三方依赖）。

- token：`base64url(payload).base64url(HMAC-SHA256(secret, payload))`，payload 含过期戳。
  纯对称签名，服务端可独立签发/校验；密钥在 ADMIN_SESSION_SECRET（env，绝不进 git）。
- 密码：env 存 PBKDF2-SHA256 哈希（`pbkdf2_sha256$iter$salt_hex$hash_hex`），不存明文；
  校验用 hmac.compare_digest 常量时间比较。
- 登录节流：同 IP 连续失败到阈值后短时锁定，缓解爆破（进程内，重启即清，可接受）。
鉴权范围由 main.py 的中间件统一裁决；agent 上报端点本期不在此校验内（保持开放）。
"""
from __future__ import annotations

import base64
import hashlib
import hmac
import json
import logging
import secrets
import threading
import time

from .config import settings

logger = logging.getLogger(__name__)

TOKEN_TTL_SECONDS = 7 * 24 * 3600  # 后台登录有效期 7 天
_PBKDF2_ITERS = 200_000

# 登录节流参数
_MAX_FAILS = 5             # 连续失败到此值触发锁定
_LOCK_SECONDS = 60         # 锁定时长（秒）
_FAIL_WINDOW = 300         # 失败计数滑动窗口（秒）


# ---------------------------------------------------------------------------
# base64url（去 padding）
# ---------------------------------------------------------------------------
def _b64e(raw: bytes) -> str:
    return base64.urlsafe_b64encode(raw).decode("ascii").rstrip("=")


def _b64d(s: str) -> bytes:
    return base64.urlsafe_b64decode(s + "=" * (-len(s) % 4))


def _secret_bytes() -> bytes:
    return (settings.admin_session_secret or "").encode("utf-8")


# ---------------------------------------------------------------------------
# token 签发 / 校验
# ---------------------------------------------------------------------------
def create_token(ttl_seconds: int = TOKEN_TTL_SECONDS) -> str:
    payload = _b64e(json.dumps({"exp": int(time.time()) + ttl_seconds}).encode("utf-8"))
    sig = _b64e(hmac.new(_secret_bytes(), payload.encode("ascii"), hashlib.sha256).digest())
    return f"{payload}.{sig}"


def verify_token(token: str | None) -> bool:
    """签名有效且未过期 → True。密钥未配置（空）时一律 False（安全失败）。"""
    if not token or not settings.admin_session_secret:
        return False
    try:
        payload, sig = token.split(".", 1)
        expected = _b64e(hmac.new(_secret_bytes(), payload.encode("ascii"), hashlib.sha256).digest())
        if not hmac.compare_digest(sig, expected):
            return False
        data = json.loads(_b64d(payload))
        return int(data.get("exp", 0)) > int(time.time())
    except Exception:  # noqa: BLE001  任何解析异常都视为无效 token
        return False


# ---------------------------------------------------------------------------
# 密码哈希 / 校验
# ---------------------------------------------------------------------------
def hash_password(password: str, iters: int = _PBKDF2_ITERS) -> str:
    """生成 `pbkdf2_sha256:iter:salt_hex:hash_hex`，供写入 ADMIN_PASSWORD_HASH。

    用 `:` 分隔（不用 `$`）以免落 .env 时被变量展开误伤。
    """
    salt = secrets.token_bytes(16)
    dk = hashlib.pbkdf2_hmac("sha256", password.encode("utf-8"), salt, iters)
    return f"pbkdf2_sha256:{iters}:{salt.hex()}:{dk.hex()}"


def verify_password(password: str) -> bool:
    stored = settings.admin_password_hash or ""
    try:
        algo, iters_s, salt_hex, hash_hex = stored.split(":")
        if algo != "pbkdf2_sha256":
            return False
        dk = hashlib.pbkdf2_hmac(
            "sha256", password.encode("utf-8"), bytes.fromhex(salt_hex), int(iters_s)
        )
        return hmac.compare_digest(dk.hex(), hash_hex)
    except Exception:  # noqa: BLE001
        return False


def is_configured() -> bool:
    """密码哈希与签名密钥均已设置才算配置完成。"""
    return bool(settings.admin_password_hash and settings.admin_session_secret)


# ---------------------------------------------------------------------------
# 鉴权放行裁决（中间件与冒烟共用，单一事实源）
# ---------------------------------------------------------------------------
def is_open_path(method: str, path: str) -> bool:
    """True = 无需后台 token 即可访问。

    放行：OPTIONS 预检 / /health / {prefix}/auth/login / 非业务前缀(/docs 等) /
    agent 端点子集（POST .../activity/report、POST .../windows/report、任意 .../agent/*）。
    其余 {prefix}/* 一律需要后台 token。
    """
    p = settings.api_v1_prefix
    if method == "OPTIONS":
        return True  # CORS 预检无 Authorization，必须放行
    if path == "/health" or path == f"{p}/auth/login":
        return True
    if not path.startswith(f"{p}/"):
        return True  # /docs、/openapi.json 等非业务前缀，暂不在本期范围
    if method == "POST" and path in (f"{p}/activity/report", f"{p}/windows/report"):
        return True
    if path.startswith(f"{p}/agent/"):  # /agent/health + /agent/updates/*
        return True
    return False


# ---------------------------------------------------------------------------
# 登录失败节流（进程内）
# ---------------------------------------------------------------------------
_lock = threading.Lock()
_fails: dict[str, tuple[int, float]] = {}  # ip -> (count, first_fail_ts)
_locked_until: dict[str, float] = {}       # ip -> unlock_ts


def throttle_retry_after(ip: str) -> int:
    """该 IP 当前是否被锁；返回需等待秒数（0 = 未锁）。"""
    now = time.time()
    with _lock:
        until = _locked_until.get(ip, 0)
        return max(0, int(until - now)) if until > now else 0


def record_fail(ip: str) -> None:
    now = time.time()
    with _lock:
        count, first = _fails.get(ip, (0, now))
        if now - first > _FAIL_WINDOW:
            count, first = 0, now  # 窗口过期，重新计
        count += 1
        _fails[ip] = (count, first)
        if count >= _MAX_FAILS:
            _locked_until[ip] = now + _LOCK_SECONDS
            _fails[ip] = (0, now)
            logger.warning("后台登录失败达阈值，临时锁定 IP=%s %ss", ip, _LOCK_SECONDS)


def reset_fails(ip: str) -> None:
    with _lock:
        _fails.pop(ip, None)
        _locked_until.pop(ip, None)

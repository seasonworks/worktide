"""#4 · 后台登录路由：密码 → 签发 token。"""
import logging

from fastapi import APIRouter, HTTPException, Request
from pydantic import BaseModel, Field

from .. import auth

logger = logging.getLogger(__name__)

router = APIRouter(prefix="/auth", tags=["auth"])


class LoginIn(BaseModel):
    password: str = Field(min_length=1, max_length=256)


class LoginOut(BaseModel):
    token: str
    expires_in: int


def _client_ip(request: Request) -> str:
    # nginx 经 CF-Connecting-IP 还原真实 IP；回退 X-Forwarded-For / 直连
    cf = request.headers.get("cf-connecting-ip")
    if cf:
        return cf.strip()
    xff = request.headers.get("x-forwarded-for")
    if xff:
        return xff.split(",")[0].strip()
    return request.client.host if request.client else "unknown"


@router.post("/login", response_model=LoginOut, summary="后台登录")
def login(body: LoginIn, request: Request) -> LoginOut:
    ip = _client_ip(request)

    if not auth.is_configured():
        # env 未设密码/密钥：明确报 503 便于运维定位（而非误判密码错）
        logger.error("后台鉴权未配置（ADMIN_PASSWORD_HASH / ADMIN_SESSION_SECRET 缺失）")
        raise HTTPException(status_code=503, detail="后台鉴权未配置，请联系管理员")

    wait = auth.throttle_retry_after(ip)
    if wait > 0:
        raise HTTPException(
            status_code=429,
            detail=f"登录失败过多，请 {wait} 秒后再试",
            headers={"Retry-After": str(wait)},
        )

    if auth.verify_password(body.password):
        auth.reset_fails(ip)
        logger.info("后台登录成功 IP=%s", ip)
        return LoginOut(token=auth.create_token(), expires_in=auth.TOKEN_TTL_SECONDS)

    auth.record_fail(ip)
    logger.warning("后台登录失败 IP=%s", ip)
    raise HTTPException(status_code=401, detail="密码错误")

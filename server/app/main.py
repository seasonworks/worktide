import os
from contextlib import asynccontextmanager

from fastapi import FastAPI, Request
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import JSONResponse

from . import auth, models  # noqa: F401  确保 ORM 模型在建表前完成注册
from .cleanup import cleanup_service
from .config import settings
from .database import Base, engine
from .logging_config import setup_logging
from .routers import (
    activity,
    agent_updates,
    auth as auth_router,  # #4 · 后台登录
    device_health,
    employees,
    settings as settings_router,  # Phase 6.5A · General Settings 路由
    telegram_admin,
    windows,
    work,
    work_debug,
)
from .services.clock_reminder_scheduler import clock_reminder_scheduler  # Phase 6.5A
from .services.sweeper import break_sweeper
from .services.telegram_poller import telegram_poller


@asynccontextmanager
async def lifespan(app: FastAPI):
    # 启动顺序：清理 → break 清扫 → clock reminder → Telegram poller；关闭逆序
    cleanup_service.start()
    break_sweeper.start()
    clock_reminder_scheduler.start()  # Phase 6.5A · GS-6 / GS-7
    telegram_poller.start()
    try:
        yield
    finally:
        telegram_poller.stop()
        clock_reminder_scheduler.stop()
        break_sweeper.stop()
        cleanup_service.stop()


def _ensure_sqlite_dir() -> None:
    """SQLite 文件所在目录不存在时自动创建。"""
    if not settings.database_url.startswith("sqlite"):
        return
    db_path = settings.database_url.split("///")[-1]
    if db_path in ("", ":memory:"):
        return
    directory = os.path.dirname(os.path.abspath(db_path))
    os.makedirs(directory, exist_ok=True)


def create_app() -> FastAPI:
    setup_logging()  # 唯一日志初始化点，须在其它逻辑之前
    _ensure_sqlite_dir()
    # 开发期直接建表；生产环境建议改用 Alembic 迁移
    Base.metadata.create_all(bind=engine)

    # docs_enabled=False（生产默认）时彻底移除 docs/redoc/openapi 路由，
    # 而非靠鉴权挡——这些路由不带 /api/v1 前缀，不受 require_admin_auth 保护。
    docs_kwargs = (
        {}
        if settings.docs_enabled
        else {"docs_url": None, "redoc_url": None, "openapi_url": None}
    )
    app = FastAPI(
        title=settings.app_name, version="0.1.0", lifespan=lifespan, **docs_kwargs
    )

    # ------------------------------------------------------------------
    # #4 · 应用层后台鉴权中间件（统一收口，不逐端点改）
    # 放行：OPTIONS 预检 / /health / {prefix}/auth/login / agent 端点子集；
    # 其余 {prefix}/* 必须带合法 Bearer token，否则 401。
    # agent 端点（本期保持开放，#3 再加令牌）：
    #   POST {p}/activity/report、POST {p}/windows/report、任意 {p}/agent/*
    # ------------------------------------------------------------------
    @app.middleware("http")
    async def require_admin_auth(request: Request, call_next):
        if not auth.is_open_path(request.method, request.url.path):
            header = request.headers.get("authorization", "")
            token = header[7:].strip() if header.lower().startswith("bearer ") else None
            if not auth.verify_token(token):
                return JSONResponse(
                    status_code=401, content={"detail": "未认证或登录已过期"}
                )
        return await call_next(request)

    app.add_middleware(
        CORSMiddleware,
        allow_origins=settings.cors_origins,
        allow_credentials=True,
        allow_methods=["*"],
        allow_headers=["*"],
    )

    app.include_router(auth_router.router, prefix=settings.api_v1_prefix)
    app.include_router(activity.router, prefix=settings.api_v1_prefix)
    app.include_router(employees.router, prefix=settings.api_v1_prefix)
    app.include_router(work.router, prefix=settings.api_v1_prefix)
    app.include_router(telegram_admin.router, prefix=settings.api_v1_prefix)
    app.include_router(windows.router, prefix=settings.api_v1_prefix)
    # Phase 5.3 · Device Health: /agent/health 上报 + /devices 列表/详情
    app.include_router(device_health.router, prefix=settings.api_v1_prefix)
    # Phase 5.4 · Auto Update: /agent/updates/check + /manifest + /download
    app.include_router(agent_updates.router, prefix=settings.api_v1_prefix)
    # Phase 6.5A · General Settings: /settings 列表 / PATCH / reset
    app.include_router(settings_router.router, prefix=settings.api_v1_prefix)
    # 开发期调试接口；生产可注释掉此行停用
    app.include_router(work_debug.router, prefix=settings.api_v1_prefix)

    @app.get("/health", tags=["system"], summary="健康检查")
    def health() -> dict:
        return {"status": "ok", "app": settings.app_name}

    return app


app = create_app()

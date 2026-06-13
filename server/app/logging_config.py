"""集中式日志配置（纯标准库 logging，不引入额外框架）。

- 控制台 + 轮转文件双输出，统一格式：时间 / level / 模块 / 消息
- 级别、目录、轮转参数均来自 .env
- 防重复初始化与重复 handler
- uvicorn 日志并入 root，风格统一
- 日志写入失败（磁盘满 / 权限）不影响主流程
"""
import logging
import os
from logging.handlers import RotatingFileHandler

from .config import settings

# 关闭 logging 自身的异常抛出：emit 出错（如磁盘满）时静默降级，绝不影响主流程
logging.raiseExceptions = False

_FORMAT = "%(asctime)s [%(levelname)s] %(name)s: %(message)s"
_DATEFMT = "%Y-%m-%d %H:%M:%S"

_configured = False


def _resolve_level() -> int:
    return getattr(logging, settings.log_level.upper(), logging.INFO)


def setup_logging() -> None:
    """初始化全局日志。唯一调用点：create_app() 首行。重复调用为空操作。"""
    global _configured
    if _configured:
        return

    formatter = logging.Formatter(_FORMAT, datefmt=_DATEFMT)

    # 控制台始终可用
    console = logging.StreamHandler()
    console.setFormatter(formatter)
    handlers: list[logging.Handler] = [console]

    # 文件日志：建目录/打开失败则降级为仅控制台，不让主流程崩溃
    file_error: Exception | None = None
    try:
        os.makedirs(settings.log_dir, exist_ok=True)
        file_handler = RotatingFileHandler(
            os.path.join(settings.log_dir, settings.log_file),
            maxBytes=settings.log_file_max_bytes,
            backupCount=settings.log_backup_count,
            encoding="utf-8",
            delay=True,  # 延迟到首次写入再打开文件
        )
        file_handler.setFormatter(formatter)
        handlers.append(file_handler)
    except OSError as exc:  # 磁盘满 / 无权限等
        file_error = exc

    root = logging.getLogger()
    root.setLevel(_resolve_level())
    # 先清空已有 handler，避免重复输出（--reload / 重复导入 / 重复初始化）
    for h in list(root.handlers):
        root.removeHandler(h)
    for h in handlers:
        root.addHandler(h)

    # 让 uvicorn 的日志汇入 root，统一格式
    for name in ("uvicorn", "uvicorn.error", "uvicorn.access"):
        lg = logging.getLogger(name)
        lg.handlers = []
        lg.propagate = True

    _configured = True

    log = logging.getLogger(__name__)
    if file_error is None:
        log.info(
            "日志系统已初始化：level=%s file=%s (max %s bytes x %s)",
            settings.log_level,
            os.path.join(settings.log_dir, settings.log_file),
            settings.log_file_max_bytes,
            settings.log_backup_count,
        )
    else:
        log.warning("文件日志初始化失败，降级为仅控制台输出：%s", file_error)

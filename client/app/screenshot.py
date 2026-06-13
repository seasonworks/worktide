"""预留：未来的屏幕截图功能。

当前为占位实现，默认禁用（config.screenshot.enabled = false）。

未来实现思路：
- 用 mss 截取所有显示器，Pillow 压缩为 JPEG/WebP
- 通过 multipart POST 上传到服务端（如 /api/v1/activity/screenshot）
- 上传频率由 config.screenshot.interval_seconds 控制
- 服务端落库 screenshots 表，后台分页查看
"""
import logging

from .config import ScreenshotConfig

logger = logging.getLogger(__name__)


def is_available() -> bool:
    """检查截图所需依赖是否已安装。"""
    try:
        import mss  # noqa: F401

        return True
    except ImportError:
        return False


def capture() -> bytes | None:
    """截取屏幕并返回图像字节。占位实现，当前返回 None。"""
    logger.debug("截图功能为预留接口，尚未实现")
    return None


class ScreenshotService:
    """按配置周期截图的服务骨架，便于未来接入主循环。"""

    def __init__(self, config: ScreenshotConfig) -> None:
        self.config = config

    @property
    def enabled(self) -> bool:
        return self.config.enabled and is_available()

    def maybe_capture(self) -> bytes | None:
        if not self.enabled:
            return None
        return capture()

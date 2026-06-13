import json
import os
import sys
from dataclasses import dataclass, field, fields
from pathlib import Path

APP_NAME = "EmployeeAgent"


def app_dir() -> Path:
    """返回程序所在目录（只读区）。

    - 源码运行：client/ 目录
    - PyInstaller 打包后：EXE 所在目录（如 Program Files）
    """
    if getattr(sys, "frozen", False):
        return Path(sys.executable).resolve().parent
    return Path(__file__).resolve().parents[1]


def data_dir() -> Path:
    """返回可写数据目录（存放 config.json 与 logs）。

    - 打包后（frozen）：机器级 %PROGRAMDATA%\\EmployeeAgent（所有用户可写，
      程序本体通常装在只读的 Program Files）
    - 源码运行：项目目录，保持开发期行为不变
    - 创建失败时回退到程序目录，避免崩溃
    """
    if getattr(sys, "frozen", False):
        base = Path(os.environ.get("PROGRAMDATA", r"C:\ProgramData")) / APP_NAME
    else:
        base = app_dir()
    try:
        base.mkdir(parents=True, exist_ok=True)
    except OSError:
        base = app_dir()
    return base


@dataclass
class ScreenshotConfig:
    """截图配置（预留，当前默认禁用）。"""

    enabled: bool = False
    interval_seconds: int = 300


@dataclass
class WindowTrackingConfig:
    """Phase 4.1 · 窗口活动追踪配置。默认 enabled=False，需明确启用。

    与 Reporter / Presence Layer 完全解耦，独立线程独立 buffer。
    """

    enabled: bool = False
    sample_interval_seconds: int = 2
    idle_threshold_seconds: int = 60
    flush_interval_seconds: int = 15
    title_max_length: int = 200
    upload_interval_seconds: int = 30
    upload_batch_size: int = 500
    upload_backoff_seconds: list[int] = field(
        default_factory=lambda: [5, 30, 120, 300]
    )
    # 同一 event 连续 422 多少次后 quarantine 为 invalid（避免坏数据无限重试）
    upload_max_attempts: int = 5
    buffer_max_rows: int = 100000
    buffer_retention_hours: int = 24
    # 空字符串 = window_buffer.py 自动落在 data_dir()/data/window_buffer.db
    buffer_path: str = ""


@dataclass
class WatchdogConfig:
    """Phase 5.2 · 进程内 watchdog 配置。

    阈值是 *上限*，实测心跳期望频率远低于此 —— 留充足容差避免误杀。
    决策 D3：config.json 默认 *不写* watchdog 段，靠这里的默认值生效；
    部署需要调整时再显式写 ``"watchdog": {...}``。
    """

    #: 整个 watchdog 子系统的总开关；False = 不启 daemon 线程、心跳变 noop
    enabled: bool = True

    check_interval_seconds: int = 60

    #: tracker 期望 ≤ 20s 一次 tick，给 4.5× 容差
    tracker_threshold_seconds: int = 90
    #: uploader 最长 upload_backoff_seconds=300 + drain timeout 10s ≈ 310s
    #: worst case；360 留 50s 缓冲。不要设太小，否则网络长时不通时误杀。
    uploader_threshold_seconds: int = 360
    #: agent_loop 期望 ≤ 40s 一次（report_interval=30 + timeout=10）
    agent_loop_threshold_seconds: int = 180

    #: 连续 N 次 miss 才硬退（D2）。默认 2：阈值 + check_interval 的窗口
    #: 内必须持续异常才退，过滤一次性抖动
    miss_count_to_exit: int = 2

    #: heartbeat.json 节流写盘周期（D1）
    heartbeat_file_throttle_seconds: int = 30


@dataclass
class HealthConfig:
    """Phase 5.3 · Device Health 心跳上报配置。

    决策 D1：默认 120s 一次上报（activity 30s 已经承担 last_seen 角色，
    health 是"重运维数据"，频率低一档对带宽友好）。
    决策 D3：config.json 默认 *不写* health 段，靠这里的默认值生效。
    """

    enabled: bool = True
    health_api_path: str = "/api/v1/agent/health"
    upload_interval_seconds: int = 120
    request_timeout_seconds: int = 10
    # 上报失败时的退避（与 window_uploader 同结构，避免维护两套）
    upload_backoff_seconds: list[int] = field(
        default_factory=lambda: [5, 30, 120, 300]
    )


@dataclass
class UpdateConfig:
    """Phase 5.4 · Auto Update 配置。

    决策 D2: 默认 3600s check（每小时）。
    决策 D5: 仅 stable channel，但保留字段以备 v2 扩展。
    决策 D6: HMAC secret 与服务端 ``update_hmac_secret`` 必须一致；
    decision D3: 同 watchdog/health，config.json 默认不写本段。
    """

    enabled: bool = True
    check_interval_seconds: int = 3600
    # 启动后等 N 秒再首次 check，让 HealthReporter 先注册 device_health 行
    initial_delay_seconds: int = 60
    channel: str = "stable"
    # 与 server.config.update_hmac_secret 必须一致；默认值仅供本地开发
    hmac_secret: str = "change-me-in-production"


@dataclass
class Config:
    server_url: str = "http://localhost:9000"
    api_path: str = "/api/v1/activity/report"
    windows_api_path: str = "/api/v1/windows/report"
    report_interval_seconds: int = 30
    request_timeout_seconds: int = 10
    employee_name: str = ""
    screenshot: ScreenshotConfig = field(default_factory=ScreenshotConfig)
    window: WindowTrackingConfig = field(default_factory=WindowTrackingConfig)
    # Phase 5.2 · D3: config.json 缺省时全用 WatchdogConfig() 默认值
    watchdog: "WatchdogConfig" = field(default_factory=lambda: WatchdogConfig())
    # Phase 5.3 · D3: 同上策略
    health: "HealthConfig" = field(default_factory=lambda: HealthConfig())
    # Phase 5.4 · 同上策略
    update: "UpdateConfig" = field(default_factory=lambda: UpdateConfig())

    @property
    def health_url(self) -> str:
        return self.server_url.rstrip("/") + self.health.health_api_path

    @property
    def report_url(self) -> str:
        return self.server_url.rstrip("/") + self.api_path

    @property
    def windows_report_url(self) -> str:
        return self.server_url.rstrip("/") + self.windows_api_path


def _resolve_config_path() -> Path:
    """优先数据目录的 config.json；不存在则回退程序目录（便携/开发）。"""
    candidate = data_dir() / "config.json"
    if candidate.exists():
        return candidate
    return app_dir() / "config.json"


def load_config(path: Path | None = None) -> Config:
    """从 config.json 读取配置；文件缺失或字段缺省时使用默认值。"""
    path = path or _resolve_config_path()

    data: dict = {}
    if path.exists():
        with path.open("r", encoding="utf-8") as f:
            data = json.load(f)

    screenshot = ScreenshotConfig(**(data.pop("screenshot", None) or {}))
    window = WindowTrackingConfig(**(data.pop("window", None) or {}))
    # Phase 5.2: 缺省段 → 全默认；只接受 dataclass 已知字段，未知键安全忽略
    watchdog_raw = data.pop("watchdog", None) or {}
    watchdog_known = {f.name for f in fields(WatchdogConfig)}
    watchdog = WatchdogConfig(
        **{k: v for k, v in watchdog_raw.items() if k in watchdog_known}
    )

    # Phase 5.3: 同上策略
    health_raw = data.pop("health", None) or {}
    health_known = {f.name for f in fields(HealthConfig)}
    health = HealthConfig(
        **{k: v for k, v in health_raw.items() if k in health_known}
    )

    # Phase 5.4: 同上策略
    update_raw = data.pop("update", None) or {}
    update_known = {f.name for f in fields(UpdateConfig)}
    update = UpdateConfig(
        **{k: v for k, v in update_raw.items() if k in update_known}
    )

    known = {f.name for f in fields(Config)} - {
        "screenshot", "window", "watchdog", "health", "update"
    }
    filtered = {k: v for k, v in data.items() if k in known}
    return Config(
        screenshot=screenshot,
        window=window,
        watchdog=watchdog,
        health=health,
        update=update,
        **filtered,
    )

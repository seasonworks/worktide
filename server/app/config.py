from functools import lru_cache

from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    """全局配置。所有字段都可通过环境变量或 .env 覆盖。"""

    model_config = SettingsConfigDict(
        env_file=".env",
        env_file_encoding="utf-8",
        extra="ignore",
    )

    app_name: str = "Worktide API"
    api_v1_prefix: str = "/api/v1"

    # Swagger / ReDoc / OpenAPI schema 开关。默认关闭（生产不暴露 API 结构，
    # 这些路由未走 /api/v1 前缀、不受后台鉴权中间件保护）。本地开发可 .env 设
    # DOCS_ENABLED=true 打开 /docs、/redoc、/openapi.json。
    docs_enabled: bool = False

    # #4 · 应用层后台鉴权（生产必须经 env 设置；空 = 鉴权未配置，后台一律拒绝访问）
    # ADMIN_PASSWORD_HASH：PBKDF2-SHA256 哈希串（见 app/auth.hash_password），不存明文
    # ADMIN_SESSION_SECRET：token HMAC 签名密钥（32 字节随机，绝不进 git）
    admin_password_hash: str = ""
    admin_session_secret: str = ""

    # 数据库连接串。默认 SQLite；后期可改为 postgresql+psycopg://...
    database_url: str = "sqlite:///./data/employees.db"

    # 挂机判定阈值（秒），默认 15 分钟
    idle_threshold_seconds: int = 15 * 60

    # 多久没收到上报视为离线（秒），默认 90 秒（约错过 3 次上报）
    offline_threshold_seconds: int = 90

    # 客户端上报间隔（秒），仅作参考与文档用途
    report_interval_seconds: int = 30

    # 前端后台允许的跨域来源
    cors_origins: list[str] = [
        "http://localhost:3000",
        "http://localhost:5173",
    ]

    # Telegram 挂机通知
    telegram_enabled: bool = False
    telegram_bot_token: str = ""
    telegram_chat_id: str = ""
    telegram_cooldown_seconds: int = 30 * 60  # 同一员工最短通知间隔，防刷屏
    telegram_timeout_seconds: int = 10

    # Telegram 长轮询 poller / 群面板
    telegram_poll_timeout_seconds: int = 30     # getUpdates 长轮询等待
    telegram_poll_backoff_seconds: int = 5      # 出错重试退避
    telegram_skip_backlog_on_start: bool = True  # 首启跳过历史积压，避免重放旧按钮
    telegram_send_menu_on_start: bool = False    # 启动是否自动向群发面板

    # 历史记录保留与清理（避免 SQLite 无限增长）
    log_retention_days: int = 40  # 保留最近 40 天，便于跨月统计与回溯
    cleanup_interval_seconds: int = 24 * 60 * 60  # 每天清理一次
    cleanup_batch_size: int = 5000  # 分批删除，单批行数，避免长时间持写锁
    # 查询接口返回条数上限，防止一次拉取过多
    max_query_limit: int = 500

    # 考勤 / Break 运行期默认值（settings 表可覆盖；此处为 .env 种子 / 回退默认）
    # 注意：业务逻辑一律经 settings_service 读取，不直接硬编码这些值
    break_meal_max_seconds: int = 60 * 60      # 吃饭 60 分钟
    break_toilet_max_seconds: int = 15 * 60    # 厕所 15 分钟
    break_smoke_max_seconds: int = 10 * 60     # 抽烟 10 分钟
    break_pauses_detection: bool = True        # break 期间是否暂停 idle/offline 告警
    break_sweep_interval_seconds: int = 60      # break 超时清扫周期
    # GS-13 · 休息超时后是否自动结束 break 并恢复 working。
    #   True（默认）：sweeper 自动回座 + 发 "已自动返回工作岗位" 通知（原行为）
    #   False：保持 break 状态不动，仅发 "请员工尽快返回工作岗位" 通知，等员工手动回座
    auto_return_after_break_timeout: bool = True

    # 窗口活动追踪（Phase 4.1）
    window_report_max_batch: int = 500          # /windows/report 单批最大 event 数
    window_title_max_length: int = 120          # window_sessions.window_title 截断长度
    window_raw_retention_days: int = 7          # window_events_raw 保留天数
    window_sessions_retention_days: int = 40    # window_sessions 保留天数

    # 日志系统（控制台 + 轮转文件）
    log_level: str = "INFO"
    log_dir: str = "logs"
    log_file: str = "server.log"
    log_file_max_bytes: int = 10 * 1024 * 1024  # 单文件 10 MB
    log_backup_count: int = 7  # 保留 7 个历史文件

    # Phase 5.4 · Auto Update
    # release 包根目录；服务端按 ``<release_dir>/<version>/`` 组织
    update_release_dir: str = "./data/releases"
    # HMAC-SHA256 共享密钥。**生产必须设置非默认值**；默认值仅供本地开发
    # 与客户端 config.json 的 update.hmac_secret 必须一致，否则 verify_hmac 失败
    update_hmac_secret: str = "change-me-in-production"
    # 唯一支持的 channel；架构预留，D5 决策 v1 仅 stable
    update_supported_channels: list[str] = ["stable"]

    # Phase 6.0D · Device Identity Hardening
    # 严格模式：同 machine_id 但 hw_fingerprint 不匹配时 → 拒绝上报（HTTP 409）
    # 默认 false（loose）：仅记 clone_suspect_count + 告警日志，仍接受上报。
    # 灰度观察 N 天稳定后，由运维改 .env 切 true。
    device_identity_strict_mode: bool = False

    # ------------------------------------------------------------------
    # Phase 6.5A · General Settings（统一配置中心）
    # 这里只放种子默认值；运行期 settings 表覆盖优先（见 services/settings_service.py）。
    # 业务代码一律经 settings_service.get(...) 读取，不要直接引用 settings.* 这些字段
    # —— 这是为了让 admin UI 改动能立刻生效（cache invalidate），同时保留 .env 回退。
    # ------------------------------------------------------------------

    # GS-3/4/5/7B 通知开关（UI 可改，默认全开；Telegram 总开关仍由 telegram_enabled 决定）
    notify_break_timeout_enabled: bool = True   # GS-3 自动回座通知
    notify_break_switch_enabled: bool = True    # GS-7B 切换休息类型通知（review #7 新增）
    notify_idle_enter_enabled: bool = True      # GS-4 进入挂机通知（替代以前裸 telegram_enabled）
    notify_idle_exit_enabled: bool = True       # GS-5 恢复工作通知

    # GS-5 cooldown：员工短时间内挂机→恢复→挂机→恢复刷屏防抖。不进 UI（review #3 锁定 10 分钟）
    notify_idle_exit_cooldown_seconds: int = 10 * 60

    # GS-6/7 上下班提醒：默认 OFF（review #4 防上线日全员爆群）
    notify_clock_in_reminder_enabled: bool = False
    notify_clock_out_reminder_enabled: bool = False
    clock_in_reminder_time: str = "09:00"        # "HH:MM" 24 小时；按 reminders_timezone_offset_hours 转 UTC tick
    clock_out_reminder_time: str = "18:00"
    reminders_timezone_offset_hours: int = 8     # 中国 UTC+8；范围 [-12, 14]

    # GS-11 · Clock Reminder 群发优化
    #   group（默认）：上班提前 pre_clock_in_minutes 分钟群发一次；下班准点群发一次；不按员工逐个
    #   individual：保留 6.5A 原行为（按员工逐个发）
    clock_reminder_mode: str = "group"
    pre_clock_in_minutes: int = 15               # group 模式上班提醒提前量（分钟）

    # 6.5B · Mention：管理员 Telegram user_id 列表（逗号分隔）；仅挂机事件 @管理员
    admin_telegram_user_ids: str = ""

    # scheduler 内部参数（不进 UI / RUNTIME_KEYS）：每 60 秒检查一次"是否到点"
    clock_reminder_check_interval_seconds: int = 60


@lru_cache
def get_settings() -> Settings:
    return Settings()


settings = get_settings()

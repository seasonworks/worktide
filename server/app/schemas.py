from datetime import datetime
from typing import Any

from pydantic import BaseModel, ConfigDict, Field

from .constants import EmployeeStatus


class ActivityReport(BaseModel):
    """客户端每 30 秒上报的载荷。"""

    machine_id: str = Field(..., max_length=128, description="客户端机器唯一标识")
    name: str | None = Field(None, max_length=64, description="员工姓名（首次上报用于自动注册）")
    hostname: str | None = Field(None, max_length=128, description="计算机名")
    idle_seconds: int = Field(0, ge=0, description="当前已连续空闲的秒数")
    is_active: bool = Field(True, description="本次上报周期内是否检测到键鼠活动")
    # Phase 6.0D · 设备身份硬化（可选；老 client ≤0.5.x 不带，行为完全向后兼容）
    hw_fingerprint: str | None = Field(
        None, max_length=64,
        description="sha256(CSP_UUID|BIOS_SN|BaseBoard_SN) 截断 32 字符；仅克隆检测用",
    )
    legacy_machine_id: str | None = Field(
        None, max_length=128,
        description="0.6.0 首启迁移用：上报旧 MachineGuid 让服务端把 machine_id 改写",
    )


class ReportResponse(BaseModel):
    """上报接口的返回。"""

    employee_id: int
    status: EmployeeStatus
    is_idle_alert: bool = Field(description="是否已达到挂机告警阈值")
    server_time: datetime


class EmployeeOut(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    id: int
    machine_id: str
    name: str
    hostname: str | None
    status: EmployeeStatus
    idle_seconds: int
    last_seen: datetime | None
    created_at: datetime
    is_active: bool = True
    deleted_at: datetime | None = None


class EmployeeUpdate(BaseModel):
    """员工可编辑字段（目前仅姓名）。"""

    name: str = Field(..., min_length=1, max_length=64, description="员工姓名")


class ActivityLogOut(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    id: int
    employee_id: int
    status: EmployeeStatus
    is_active: bool
    idle_seconds: int
    reported_at: datetime


class RecentActivityOut(BaseModel):
    """最近活动记录（含员工姓名与电脑名，便于后台展示）。"""

    id: int
    employee_id: int
    employee_name: str
    hostname: str | None
    status: EmployeeStatus
    is_active: bool
    idle_seconds: int
    reported_at: datetime


# ---- 第二阶段：工时 / Break（debug + 查询 API） ----


class ActionResultOut(BaseModel):
    """状态机动作结果（对应 services.work_state.ActionResult）。"""

    model_config = ConfigDict(from_attributes=True)

    changed: bool
    work_state: str
    code: str
    message: str


class ExpireBreaksOut(BaseModel):
    """手动触发超时清扫的结果。"""

    expired: int = Field(description="本次自动结束的 break 数量")
    details: list[ActionResultOut] = []


class BreakSessionOut(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    id: int
    employee_id: int
    shift_id: int | None
    break_type: str
    started_at: datetime
    ended_at: datetime | None
    end_reason: str | None
    auto_ended: bool
    planned_max_seconds: int
    duration_seconds: int | None
    started_from_activity_status: str | None


class ShiftSessionOut(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    id: int
    employee_id: int
    started_at: datetime
    ended_at: datetime | None
    end_reason: str | None
    duration_seconds: int | None


class WorkStatusOut(BaseModel):
    """后台首页：活动状态与 work_state 并存，break/shift 时长实时计算（秒）。"""

    employee_id: int
    name: str
    hostname: str | None
    activity_status: EmployeeStatus  # online / idle / offline
    work_state: str                  # off_shift / working / break_*
    break_type: str | None           # 在 break 时给出 meal/toilet/smoke
    current_break_seconds: int
    current_shift_seconds: int


class DailyWorkStatsOut(BaseModel):
    """按员工聚合的单日工时统计（时长单位：秒）。"""

    employee_id: int
    name: str
    date: str
    toilet_count: int
    toilet_seconds: int
    smoke_count: int
    smoke_seconds: int
    meal_count: int
    meal_seconds: int
    gross_shift_seconds: int
    break_seconds: int
    net_work_seconds: int


class ShiftStatsOut(BaseModel):
    """当前/最近一次班次的统计（时长单位：秒）。"""

    employee_id: int
    shift_started_at: datetime
    shift_ended_at: datetime | None
    is_current: bool                 # True=进行中；False=最近一次已结束班次
    gross_shift_seconds: int
    break_seconds: int
    net_work_seconds: int
    idle_seconds_total: int          # 累计挂机（窗口内 idle 近似累加，±一个上报周期）
    late_seconds: int                # 相对设置的上班时刻迟到秒数（准时/早到=0）
    toilet_count: int
    toilet_seconds: int
    smoke_count: int
    smoke_seconds: int
    meal_count: int
    meal_seconds: int


# ---- 第二阶段：Telegram 绑定管理（后台） ----


class BindRequest(BaseModel):
    """新增绑定请求。"""

    employee_id: int
    telegram_user_id: int = Field(..., description="Telegram 用户 id（数字，非用户名）")
    telegram_username: str | None = Field(None, max_length=64)


class RebindRequest(BaseModel):
    """改绑请求（换 Telegram 账号）。"""

    telegram_user_id: int = Field(..., description="新的 Telegram 用户 id")
    telegram_username: str | None = Field(None, max_length=64)


class TelegramUserOut(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    id: int
    employee_id: int
    employee_name: str | None = None
    telegram_user_id: int
    telegram_username: str | None
    created_at: datetime
    updated_at: datetime


class UnboundTelegramUserOut(BaseModel):
    """与 bot 交互过但尚未绑定的 Telegram 用户。"""

    model_config = ConfigDict(from_attributes=True)

    telegram_user_id: int
    telegram_username: str | None
    first_seen: datetime
    last_seen: datetime


# ---- 第四阶段·Phase 4.1：窗口活动追踪 ----


class WindowEventIn(BaseModel):
    """客户端上报的单条窗口会话事件（已在客户端按本地规则合并）。"""

    client_event_id: str = Field(..., min_length=1, max_length=64, description="幂等键，客户端生成 UUID")
    process_name: str = Field(..., min_length=1, max_length=255)
    window_title: str = Field("", max_length=2000)
    started_at: datetime
    ended_at: datetime
    duration_seconds: int = Field(..., ge=0)
    had_input: bool = True


class WindowReportIn(BaseModel):
    """POST /windows/report 载荷。"""

    machine_id: str = Field(..., max_length=128)
    events: list[WindowEventIn] = Field(default_factory=list, max_length=500)
    # Phase 6.0D · 设备身份硬化（可选；server 当前只接受不校验。识别迁移走
    # /activity/report 主路径；此处仅为 schema 一致 + 未来扩展预留）
    hw_fingerprint: str | None = Field(None, max_length=64)
    legacy_machine_id: str | None = Field(None, max_length=128)


class WindowReportOut(BaseModel):
    accepted: int = Field(description="新写入 raw 的事件数")
    deduped: int = Field(description="因 client_event_id 已存在被跳过的事件数")


class WindowSessionOut(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    id: int
    employee_id: int
    shift_id: int | None
    work_state: str
    process_name: str
    window_title: str
    normalized_title: str
    started_at: datetime
    ended_at: datetime
    duration_seconds: int


class WindowAppStatOut(BaseModel):
    """单个应用在某员工某日的时长按 work_state 拆分（秒）。"""

    process_name: str
    working_seconds: int
    break_seconds: int
    off_shift_seconds: int
    total_seconds: int


class WindowDailyStatsOut(BaseModel):
    """按员工聚合的某日窗口活动时长。"""

    employee_id: int
    name: str
    date: str
    total_working_seconds: int
    total_break_seconds: int
    total_off_shift_seconds: int
    top_apps: list[WindowAppStatOut]


# ---------------------------------------------------------------------------
# Phase 5.3 · Device Health
# ---------------------------------------------------------------------------


class HealthWatchdogIn(BaseModel):
    """客户端上报的 watchdog 子结构。原样落库到 device_health.watchdog_json，
    服务端不做语义解释（D6：状态推断只看顶层字段，watchdog 只用于详情展示）。"""

    enabled: bool = True
    thresholds_seconds: dict[str, int] = {}
    ages_seconds: dict[str, float] = {}
    misses: dict[str, int] = {}


class HealthUpdateIn(BaseModel):
    """Phase 5.4 · 客户端上报的升级子状态。

    status 枚举: idle | checking | downloading | staged | installing | failed
    """

    status: str = "idle"
    target_version: str | None = None
    last_check_at: datetime | None = None
    last_error: str | None = None


class HealthReportIn(BaseModel):
    """POST /api/v1/agent/health 请求体。

    与 client/app/health_reporter.py 的 build_payload() 严格对齐；
    任何字段改动都要双向同步。
    """

    machine_id: str
    hostname: str | None = None
    agent_version: str
    process_started_at: datetime | None = None
    uptime_seconds: int = 0
    first_start_at: datetime | None = None
    last_start_at: datetime | None = None
    last_exit_at: datetime | None = None
    last_exit_reason: str | None = None
    restart_count: int = 0
    pending_events: int = 0
    watchdog: HealthWatchdogIn | None = None
    # Phase 5.4 · update 子结构；老 client 不带，server 兜底默认 idle
    update: HealthUpdateIn | None = None
    # Phase 6.0D · 设备身份硬化（可选；同 ActivityReport，向后兼容）
    hw_fingerprint: str | None = None
    legacy_machine_id: str | None = None


class HealthReportOut(BaseModel):
    """/agent/health 响应。简短确认，无业务字段。"""

    status: str = "received"


class DeviceListItemOut(BaseModel):
    """GET /api/v1/devices 列表项。

    决策（设计稿简化）：列表**不**展示 watchdog ages/misses/thresholds；
    那些只在 GET /devices/{id} 详情里返回。
    Phase 5.4 加 update_status / update_target_version：列表也展示（一列徽章）。
    """

    model_config = ConfigDict(from_attributes=True)

    employee_id: int | None
    employee_name: str | None
    machine_id: str
    hostname: str | None
    agent_version: str
    status: str  # healthy / degraded / unstable / stale / offline
    last_seen: datetime | None
    last_upload: datetime | None
    uptime_seconds: int
    restart_count: int
    last_exit_reason: str | None
    pending_events: int
    # Phase 5.4 · 列表展示用最小子集
    update_status: str = "idle"
    update_target_version: str | None = None


class DeviceDetailOut(DeviceListItemOut):
    """GET /api/v1/devices/{employee_id} 详情。列表项 + 完整时间线 + watchdog + update。"""

    process_started_at: datetime | None
    first_start_at: datetime | None
    last_start_at: datetime | None
    last_exit_at: datetime | None
    last_report_at: datetime | None
    watchdog: HealthWatchdogIn | None = None
    # Phase 5.4 · 详情展示完整 update 子结构（含 last_check_at + last_error）
    update_last_check_at: datetime | None = None
    update_last_error: str | None = None


# ---------------------------------------------------------------------------
# Phase 5.4 · Auto Update
# ---------------------------------------------------------------------------


class UpdateManifestOut(BaseModel):
    """Release manifest，对应 server/data/releases/<version>/manifest.json 内容。

    signature = HMAC-SHA256(secret, f"{version}|{sha256}|{size}")
    客户端必须 verify_sha256 + verify_hmac 双重通过 (D6)
    """

    version: str
    channel: str = "stable"
    sha256: str
    signature: str
    size: int
    released_at: datetime | None = None
    min_compat_version: str = "0.5.0"
    download_url: str
    changelog: str = ""


class UpdateCheckOut(BaseModel):
    """GET /api/v1/agent/updates/check 响应。"""

    current_version: str
    latest_version: str
    update_available: bool
    manifest: UpdateManifestOut | None = None


# ---------------------------------------------------------------------------
# Phase 6.5A · General Settings（统一配置中心）
# ---------------------------------------------------------------------------


class SettingOut(BaseModel):
    """单条设置项的当前值 + 元信息。

    value/default 用 Any，因为同一接口要同时返回 int/bool/str 三种已解析类型；
    UI 渲染时按 value_type 决定控件（Select / Switch / TimePicker）。
    """

    key: str
    value: Any
    value_type: str        # int / bool / float / str
    default: Any
    description: str = ""
    group: str = "misc"    # 见 SettingGroupOut 的 id 集合
    is_overridden: bool    # True = DB 有显式行；False = 当前是 .env / pydantic 默认


class SettingGroupOut(BaseModel):
    """设置分组（UI 渲染用）。后端只声明分组归属与 i18n label。"""

    id: str
    label_zh: str
    label_en: str
    keys: list[str]


class SettingsListOut(BaseModel):
    """GET /api/v1/settings 响应。"""

    settings: list[SettingOut]
    groups: list[SettingGroupOut]


class SettingUpdate(BaseModel):
    """PATCH /api/v1/settings/{key} 请求体。"""

    value: Any = Field(..., description="新值；按 key 对应 value_type 解析与校验")


class SettingResetOut(BaseModel):
    """POST /api/v1/settings/{key}/reset 或 POST /api/v1/settings/reset-all 响应。"""

    reset: int = Field(description="实际删除的覆盖行数")
    settings: list[SettingOut] = Field(default_factory=list, description="重置后的完整列表（便于前端一次拿全）")

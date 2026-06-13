from datetime import datetime, timezone

from sqlalchemy import JSON, BigInteger, Boolean, DateTime, ForeignKey, Integer, String, Text
from sqlalchemy.orm import Mapped, mapped_column, relationship

from .constants import EmployeeStatus, WorkState
from .database import Base


def utcnow() -> datetime:
    return datetime.now(timezone.utc)


class Employee(Base):
    """员工（以客户端机器为单位）。"""

    __tablename__ = "employees"

    id: Mapped[int] = mapped_column(primary_key=True)
    # 客户端机器唯一标识（如机器 GUID / MAC），用于识别同一台电脑
    machine_id: Mapped[str] = mapped_column(String(128), unique=True, index=True)
    name: Mapped[str] = mapped_column(String(64))
    hostname: Mapped[str | None] = mapped_column(String(128), nullable=True)

    # 最近一次上报得到的状态：online / idle（offline 在读取时按 last_seen 实时计算）
    status: Mapped[str] = mapped_column(String(16), default=EmployeeStatus.offline.value)
    idle_seconds: Mapped[int] = mapped_column(Integer, default=0)
    last_seen: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)

    # 软删除/归档：is_active=False 即业务失效（已离职），所有历史数据保留
    is_active: Mapped[bool] = mapped_column(Boolean, nullable=False, default=True)
    deleted_at: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True), nullable=True
    )

    # Phase 6.0D · Device Identity Hardening
    # ------------------------------------------------------------------
    # machine_id 在 0.6.0 之后由客户端的 ProgramData\EmployeeAgent\machine.json
    # 持久化的 UUID4 提供（不再直接读 Windows MachineGuid）。下列字段为辅助：
    #   hw_fingerprint                同 machine_id 但 hw 不同 = 克隆嫌疑
    #   hw_fingerprint_first_seen     首次入库时间，便于审计
    #   legacy_machine_id             0.6.0 首启时上报的旧 MachineGuid，做迁移审计
    #   clone_suspect_count / last_at 克隆嫌疑计数 / 最近一次发生时间
    # 全部 nullable；老 client (≤0.5.x) 不上报这些字段时这些列保持 NULL。
    hw_fingerprint: Mapped[str | None] = mapped_column(String(64), nullable=True)
    hw_fingerprint_first_seen: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True), nullable=True
    )
    legacy_machine_id: Mapped[str | None] = mapped_column(String(128), nullable=True)
    clone_suspect_count: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    clone_suspect_last_at: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True), nullable=True
    )

    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), default=utcnow, onupdate=utcnow
    )

    logs: Mapped[list["ActivityLog"]] = relationship(
        back_populates="employee", cascade="all, delete-orphan"
    )


class ActivityLog(Base):
    """每次上报落一条记录，用于历史与统计。"""

    __tablename__ = "activity_logs"

    id: Mapped[int] = mapped_column(primary_key=True)
    employee_id: Mapped[int] = mapped_column(
        ForeignKey("employees.id", ondelete="CASCADE"), index=True
    )

    status: Mapped[str] = mapped_column(String(16))  # online / idle
    is_active: Mapped[bool] = mapped_column(Boolean, default=True)
    idle_seconds: Mapped[int] = mapped_column(Integer, default=0)

    reported_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), default=utcnow, index=True
    )

    employee: Mapped["Employee"] = relationship(back_populates="logs")


# ---- 第二阶段：Telegram 打卡 / 工时 / Break（新增表，不影响以上现有表） ----


class TelegramUser(Base):
    """员工 ↔ Telegram 用户绑定。身份按 telegram_user_id 识别，不靠用户名。"""

    __tablename__ = "telegram_users"

    id: Mapped[int] = mapped_column(primary_key=True)
    employee_id: Mapped[int] = mapped_column(
        ForeignKey("employees.id", ondelete="CASCADE"), unique=True, index=True
    )
    # Telegram 用户 id 可超 32 位，用 BigInteger（SQLite 按 INTEGER 处理）
    telegram_user_id: Mapped[int] = mapped_column(BigInteger, unique=True, index=True)
    telegram_username: Mapped[str | None] = mapped_column(String(64), nullable=True)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), default=utcnow, onupdate=utcnow
    )


class EmployeeStatusRow(Base):
    """员工考勤/工时状态机的当前态（1:1）。与活动状态 employees.status 正交。"""

    __tablename__ = "employee_status"

    employee_id: Mapped[int] = mapped_column(
        ForeignKey("employees.id", ondelete="CASCADE"), primary_key=True
    )
    work_state: Mapped[str] = mapped_column(
        String(16), default=WorkState.off_shift.value
    )
    state_since: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), default=utcnow
    )
    # 当前进行中的 break（便于 O(1) 取用与超时判定）
    active_break_id: Mapped[int | None] = mapped_column(
        ForeignKey("break_sessions.id", ondelete="SET NULL"), nullable=True
    )
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), default=utcnow, onupdate=utcnow
    )


class ShiftSession(Base):
    """上班→下班一段在岗时段（用于统计真正工作时长）。"""

    __tablename__ = "shift_sessions"

    id: Mapped[int] = mapped_column(primary_key=True)
    employee_id: Mapped[int] = mapped_column(
        ForeignKey("employees.id", ondelete="CASCADE"), index=True
    )
    started_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), default=utcnow, index=True
    )
    ended_at: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True), nullable=True, index=True
    )
    end_reason: Mapped[str | None] = mapped_column(String(16), nullable=True)
    duration_seconds: Mapped[int | None] = mapped_column(Integer, nullable=True)


class BreakSession(Base):
    """每次 break（吃饭/厕所/抽烟）。支撑暂停检测、自动超时与统计。"""

    __tablename__ = "break_sessions"

    id: Mapped[int] = mapped_column(primary_key=True)
    employee_id: Mapped[int] = mapped_column(
        ForeignKey("employees.id", ondelete="CASCADE"), index=True
    )
    shift_id: Mapped[int | None] = mapped_column(
        ForeignKey("shift_sessions.id", ondelete="SET NULL"), nullable=True
    )
    break_type: Mapped[str] = mapped_column(String(16))  # meal / toilet / smoke
    started_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), default=utcnow, index=True
    )
    ended_at: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True), nullable=True, index=True
    )
    # manual_return / timeout / shift_end / switched
    end_reason: Mapped[str | None] = mapped_column(String(16), nullable=True)
    # True 表示系统强制超时结束（区别于员工主动回座），供统计区分
    auto_ended: Mapped[bool] = mapped_column(Boolean, default=False)
    # 开始时快照当时配置的上限，改配置不回溯进行中/历史 break
    planned_max_seconds: Mapped[int] = mapped_column(Integer)
    duration_seconds: Mapped[int | None] = mapped_column(Integer, nullable=True)
    # 开始 break 时员工的活动状态快照（online/idle/offline），供未来行为分析
    started_from_activity_status: Mapped[str | None] = mapped_column(
        String(16), nullable=True
    )


class Setting(Base):
    """运行期可配置项（后台设置页可改）。键值表，值统一存字符串。"""

    __tablename__ = "settings"

    key: Mapped[str] = mapped_column(String(64), primary_key=True)
    value: Mapped[str] = mapped_column(Text)
    value_type: Mapped[str] = mapped_column(String(16), default="str")  # int/bool/float/str
    description: Mapped[str | None] = mapped_column(String(255), nullable=True)
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), default=utcnow, onupdate=utcnow
    )


class TelegramSeenUser(Base):
    """与 bot 交互过的 Telegram 用户（含未绑定）。供后台绑定页挑选未绑定用户。"""

    __tablename__ = "telegram_seen_users"

    telegram_user_id: Mapped[int] = mapped_column(BigInteger, primary_key=True)
    telegram_username: Mapped[str | None] = mapped_column(String(64), nullable=True)
    first_seen: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)
    last_seen: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), default=utcnow, onupdate=utcnow
    )


# ---- 第四阶段·Phase 4.1：窗口活动追踪（Analytics Layer，与 Presence/Workforce/Lifecycle 解耦） ----


class WindowEventRaw(Base):
    """客户端上报的原始窗口会话事件（短期保留 7d，供 aggregator 处理 + 审计回放）。

    client_event_id 幂等键由客户端生成，重传安全。aggregator 处理后置 processed_at；
    cleanup 周期清理 retention 过期行。
    """

    __tablename__ = "window_events_raw"

    id: Mapped[int] = mapped_column(primary_key=True)
    employee_id: Mapped[int] = mapped_column(
        ForeignKey("employees.id", ondelete="CASCADE"), index=True
    )
    client_event_id: Mapped[str] = mapped_column(String(64), unique=True, index=True)
    process_name: Mapped[str] = mapped_column(String(255))
    # 原始 title 短期内允许全量保留以便回放；落 sessions 时截断
    window_title: Mapped[str] = mapped_column(Text, default="")
    started_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), index=True
    )
    ended_at: Mapped[datetime] = mapped_column(DateTime(timezone=True))
    duration_seconds: Mapped[int] = mapped_column(Integer)
    # 段内是否有键鼠活动（客户端判定；用于切分 idle 段）
    had_input: Mapped[bool] = mapped_column(Boolean, default=True)
    uploaded_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), default=utcnow
    )
    processed_at: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True), nullable=True
    )


class WindowSession(Base):
    """聚合后的窗口会话（长期保留，统计的事实底座）。

    aggregator 按 work_state / shift 边界切分 raw event，并尝试与最近一条 open session
    合并（同 employee/process/normalized_title/shift/work_state，时间连续 ≤ merge_gap）。
    """

    __tablename__ = "window_sessions"

    id: Mapped[int] = mapped_column(primary_key=True)
    employee_id: Mapped[int] = mapped_column(
        ForeignKey("employees.id", ondelete="CASCADE"), index=True
    )
    shift_id: Mapped[int | None] = mapped_column(
        ForeignKey("shift_sessions.id", ondelete="SET NULL"), nullable=True
    )
    # off_shift / working / break_meal / break_toilet / break_smoke
    work_state: Mapped[str] = mapped_column(String(16))
    process_name: Mapped[str] = mapped_column(String(255))
    window_title: Mapped[str] = mapped_column(String(120))
    # normalize_window_title 处理后的稳定标题，供 merge 比较与未来 category 化
    normalized_title: Mapped[str] = mapped_column(String(120), index=True)
    started_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), index=True)
    ended_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), index=True)
    duration_seconds: Mapped[int] = mapped_column(Integer)
    # 合并了多少条 raw event（含跨批次）
    source_event_count: Mapped[int] = mapped_column(Integer, default=1)


# ---------------------------------------------------------------------------
# Phase 5.3 · Device Health（仅最新快照，按 machine_id upsert）
# ---------------------------------------------------------------------------


class DeviceHealth(Base):
    """Phase 5.3 · 设备健康快照。

    设计 D3：**latest-only**，按 machine_id upsert，不保留历史。需要 trend
    时再加 device_health_history 表（Phase 5.4 候选）。

    与 Employee 的关系：通过 machine_id 反查；employee_id 是冗余外键，
    便于按员工 join 查询。machine_id 还没注册成 employee 时 employee_id 为 NULL。

    JSON 字段 watchdog_json 直接保存 client 上报的 watchdog 子结构
    （ages_seconds / misses / thresholds_seconds / enabled），不切表，
    Admin UI 详情页直接渲染。
    """

    __tablename__ = "device_health"

    id: Mapped[int] = mapped_column(primary_key=True)
    machine_id: Mapped[str] = mapped_column(String(128), unique=True, index=True)
    employee_id: Mapped[int | None] = mapped_column(
        ForeignKey("employees.id", ondelete="SET NULL"), nullable=True, index=True
    )

    hostname: Mapped[str | None] = mapped_column(String(128), nullable=True)
    agent_version: Mapped[str] = mapped_column(String(32))

    # 进程级生命周期（来源：客户端 restart_count.json + watchdog 内部 uptime）
    process_started_at: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True), nullable=True
    )
    uptime_seconds: Mapped[int] = mapped_column(Integer, default=0)
    first_start_at: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True), nullable=True
    )
    last_start_at: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True), nullable=True
    )
    last_exit_at: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True), nullable=True
    )
    last_exit_reason: Mapped[str | None] = mapped_column(String(64), nullable=True)
    restart_count: Mapped[int] = mapped_column(Integer, default=0)

    # Phase 5.3 决策追加：本地 window_buffer 中 status=pending 的事件数
    pending_events: Mapped[int] = mapped_column(Integer, default=0)

    # watchdog 子状态（ages_seconds / misses / thresholds_seconds / enabled）
    # 列表页不展示，仅在 Device Detail 页渲染（设计稿决策）
    watchdog_json: Mapped[dict | None] = mapped_column(JSON, nullable=True)

    # Phase 5.4 · 升级状态。仅最新一份；trend 留给 Phase 5.5+ 的 history 表
    # status 枚举: idle | checking | downloading | staged | installing | failed
    update_status: Mapped[str] = mapped_column(String(16), default="idle")
    update_target_version: Mapped[str | None] = mapped_column(
        String(16), nullable=True
    )
    update_last_check_at: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True), nullable=True
    )
    update_last_error: Mapped[str | None] = mapped_column(
        String(256), nullable=True
    )

    # server 收到这次上报时的时间（用于 stale/offline 判断的辅助信号）
    last_report_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), default=utcnow, index=True
    )

    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), default=utcnow
    )
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), default=utcnow, onupdate=utcnow
    )

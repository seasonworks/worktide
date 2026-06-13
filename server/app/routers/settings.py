"""Phase 6.5A · General Settings 路由。

复用既有 `settings` 表 + `settings_service`，对外暴露：

- GET    /api/v1/settings              列出所有 RUNTIME_KEYS 当前值 + 元信息 + 分组
- PATCH  /api/v1/settings/{key}        改单条；写 DB + invalidate cache
- POST   /api/v1/settings/{key}/reset  单条恢复默认（删 DB 行）
- POST   /api/v1/settings/reset-all    review #9 一键恢复全部默认

设计取舍：
- 描述 / 分组 / 写入校验规则**集中**在本文件，方便后续加 key 时只改一处
- value 用 Any，后端按 RUNTIME_KEYS[key] 解析；不可解析 422
- *_seconds 范围 [60, 86400]；*_time 严格 HH:MM；*_hours 范围 [-12, 14]
- 鉴权与项目其它 admin 接口一致（暂无；统一鉴权是路线图议题，见设计 doc R7）
"""
from __future__ import annotations

import logging
import re

from fastapi import APIRouter, Depends, HTTPException
from sqlalchemy.orm import Session

from ..database import get_db
from ..schemas import (
    SettingGroupOut,
    SettingOut,
    SettingResetOut,
    SettingsListOut,
    SettingUpdate,
)
from ..services.settings_service import RUNTIME_KEYS, settings_service

logger = logging.getLogger(__name__)

router = APIRouter(prefix="/settings", tags=["settings"])

# ---------------------------------------------------------------------------
# 元数据：分组 / 描述（前端可不依赖，但接口给出便于 i18n 与未来扩展）
# 当新增 RUNTIME_KEY 时，**同步**更新 _DESCRIPTIONS 与 _GROUPS，否则 GET 会给空描述
# 或归到 misc。两个字典都做完整性自检（见模块底部 _self_check）。
# ---------------------------------------------------------------------------

_DESCRIPTIONS: dict[str, str] = {
    "idle_threshold_seconds":            "挂机判定阈值（秒），超过则进入 Idle 状态并触发通知",
    "offline_threshold_seconds":         "多久没收到上报视为离线（秒）",
    "break_meal_max_seconds":            "吃饭最长时长（秒），超时自动回座",
    "break_toilet_max_seconds":          "上厕所最长时长（秒），超时自动回座",
    "break_smoke_max_seconds":           "抽烟最长时长（秒），超时自动回座",
    "break_pauses_detection":            "休息期间是否暂停挂机告警（仅 break_meal/toilet/smoke；off_shift 永远不参与挂机检测，不受此开关影响）",
    "auto_return_after_break_timeout":   "休息超时后是否自动结束休息并恢复工作（ON：自动回座+通知；OFF：保持休息状态，仅提醒员工手动返回）",
    "notify_break_timeout_enabled":      "自动回座时是否发送 Telegram 群内通知",
    "notify_break_switch_enabled":       "员工切换休息类型时是否发送状态通知",
    "notify_idle_enter_enabled":         "员工进入挂机状态时是否发送通知",
    "notify_idle_exit_enabled":          "员工恢复工作时是否发送通知（含 10 分钟冷却防刷屏）",
    "notify_clock_in_reminder_enabled":  "上班时间未打卡员工是否发送提醒",
    "notify_clock_out_reminder_enabled": "下班时间未下班员工是否发送提醒",
    "clock_in_reminder_time":            "上班提醒时刻 (HH:MM 24h 制)",
    "clock_out_reminder_time":           "下班提醒时刻 (HH:MM 24h 制)",
    "reminders_timezone_offset_hours":   "提醒时区相对 UTC 的小时偏移，中国 = +8",
    "clock_reminder_mode":               "上下班提醒模式：group=群发一次（默认）/ individual=按员工逐个提醒",
    "pre_clock_in_minutes":              "group 模式上班提醒提前量（分钟），到 上班时刻-该值 群发一次",
    "admin_telegram_user_ids":           "管理员 Telegram user_id 列表（逗号分隔）；仅挂机进入事件 @管理员",
}

_GROUPS: list[SettingGroupOut] = [
    SettingGroupOut(
        id="break_timeouts",
        label_zh="休息超时",
        label_en="Break Timeouts",
        keys=[
            "break_meal_max_seconds",
            "break_toilet_max_seconds",
            "break_smoke_max_seconds",
            "auto_return_after_break_timeout",
        ],
    ),
    SettingGroupOut(
        id="idle_detection",
        label_zh="挂机检测",
        label_en="Idle Detection",
        keys=["idle_threshold_seconds", "break_pauses_detection"],
    ),
    SettingGroupOut(
        id="notifications",
        label_zh="通知开关",
        label_en="Notifications",
        keys=[
            "notify_break_timeout_enabled",
            "notify_break_switch_enabled",
            "notify_idle_enter_enabled",
            "notify_idle_exit_enabled",
            "admin_telegram_user_ids",
        ],
    ),
    SettingGroupOut(
        id="clock_reminders",
        label_zh="上下班提醒",
        label_en="Clock Reminders",
        keys=[
            "clock_reminder_mode",
            "pre_clock_in_minutes",
            "notify_clock_in_reminder_enabled",
            "clock_in_reminder_time",
            "notify_clock_out_reminder_enabled",
            "clock_out_reminder_time",
            "reminders_timezone_offset_hours",
        ],
    ),
    SettingGroupOut(
        id="advanced",
        label_zh="高级",
        label_en="Advanced",
        keys=["offline_threshold_seconds"],
    ),
]

# 反查：key -> group id（_self_check 也用）
_KEY_TO_GROUP: dict[str, str] = {
    k: g.id for g in _GROUPS for k in g.keys
}

# ---------------------------------------------------------------------------
# 写入校验
# ---------------------------------------------------------------------------

_TIME_RE = re.compile(r"^([01]?\d|2[0-3]):[0-5]\d$")
# *_seconds 通用范围：1 分钟 ~ 24 小时；offline 例外不在此处放，因为 90s 默认是合规值
_SECONDS_KEYS_RANGE: dict[str, tuple[int, int]] = {
    "idle_threshold_seconds":   (60, 24 * 3600),
    "offline_threshold_seconds": (30, 3600),     # 30s ~ 1h
    "break_meal_max_seconds":    (60, 24 * 3600),
    "break_toilet_max_seconds":  (60, 24 * 3600),
    "break_smoke_max_seconds":   (60, 24 * 3600),
}


def _validate_value(key: str, value):
    """对解析后的值做语义校验。失败抛 HTTPException 422。"""
    if key in _SECONDS_KEYS_RANGE:
        lo, hi = _SECONDS_KEYS_RANGE[key]
        if not (lo <= int(value) <= hi):
            raise HTTPException(
                status_code=422,
                detail=f"{key} 必须在 [{lo}, {hi}] 秒之间",
            )
    elif key.endswith("_reminder_time"):
        if not isinstance(value, str) or not _TIME_RE.match(value):
            raise HTTPException(
                status_code=422,
                detail=f"{key} 必须是 HH:MM 24 小时制（如 09:00 / 18:30）",
            )
    elif key == "reminders_timezone_offset_hours":
        if not (-12 <= int(value) <= 14):
            raise HTTPException(
                status_code=422,
                detail="reminders_timezone_offset_hours 必须在 [-12, 14] 之间",
            )
    elif key == "clock_reminder_mode":
        if value not in ("group", "individual"):
            raise HTTPException(
                status_code=422,
                detail="clock_reminder_mode 必须是 group 或 individual",
            )
    elif key == "pre_clock_in_minutes":
        if not (0 <= int(value) <= 120):
            raise HTTPException(
                status_code=422,
                detail="pre_clock_in_minutes 必须在 [0, 120] 分钟之间",
            )
    elif key == "admin_telegram_user_ids":
        # 允许空串；非空则必须是逗号/分号分隔的整数
        raw = str(value).replace(";", ",")
        for part in raw.split(","):
            part = part.strip()
            if part and not (part.lstrip("-").isdigit()):
                raise HTTPException(
                    status_code=422,
                    detail=f"admin_telegram_user_ids 含非法 id: {part!r}（应为逗号分隔的数字）",
                )

# ---------------------------------------------------------------------------
# 序列化
# ---------------------------------------------------------------------------


def _snapshot(db: Session) -> list[SettingOut]:
    """把所有 RUNTIME_KEYS 当前状态序列化为 SettingOut 列表。

    is_overridden 通过对照 settings 表行是否存在判定；零额外开销（顺手用 db.get）。
    """
    from ..models import Setting

    out: list[SettingOut] = []
    for key, typ in RUNTIME_KEYS.items():
        current = settings_service.get(key)
        default = settings_service.default_of(key)
        overridden = db.get(Setting, key) is not None
        out.append(
            SettingOut(
                key=key,
                value=current,
                value_type=typ.__name__,
                default=default,
                description=_DESCRIPTIONS.get(key, ""),
                group=_KEY_TO_GROUP.get(key, "misc"),
                is_overridden=overridden,
            )
        )
    return out


# ---------------------------------------------------------------------------
# 端点
# ---------------------------------------------------------------------------


@router.get(
    "",
    response_model=SettingsListOut,
    summary="列出所有运行期可配置项（含当前值/默认/分组）",
)
def list_settings(db: Session = Depends(get_db)) -> SettingsListOut:
    return SettingsListOut(settings=_snapshot(db), groups=_GROUPS)


@router.patch(
    "/{key}",
    response_model=SettingOut,
    summary="更新单条设置",
)
def patch_setting(
    key: str,
    payload: SettingUpdate,
    db: Session = Depends(get_db),
) -> SettingOut:
    if key not in RUNTIME_KEYS:
        raise HTTPException(status_code=404, detail=f"未知设置项: {key}")
    try:
        parsed = settings_service.update(db, key, payload.value)
    except (ValueError, TypeError) as exc:
        raise HTTPException(
            status_code=422,
            detail=f"{key} 值无法解析为 {RUNTIME_KEYS[key].__name__}: {exc}",
        ) from exc
    _validate_value(key, parsed)
    # 由于 update 内部已 commit + invalidate，直接读最新快照即可
    from ..models import Setting
    overridden = db.get(Setting, key) is not None
    return SettingOut(
        key=key,
        value=settings_service.get(key),
        value_type=RUNTIME_KEYS[key].__name__,
        default=settings_service.default_of(key),
        description=_DESCRIPTIONS.get(key, ""),
        group=_KEY_TO_GROUP.get(key, "misc"),
        is_overridden=overridden,
    )


@router.post(
    "/{key}/reset",
    response_model=SettingResetOut,
    summary="单条恢复默认（删 DB 覆盖行，回退到 .env / 内置默认）",
)
def reset_setting(key: str, db: Session = Depends(get_db)) -> SettingResetOut:
    if key not in RUNTIME_KEYS:
        raise HTTPException(status_code=404, detail=f"未知设置项: {key}")
    from ..models import Setting
    existed = db.get(Setting, key) is not None
    settings_service.reset(db, key)
    return SettingResetOut(
        reset=1 if existed else 0,
        settings=_snapshot(db),
    )


@router.post(
    "/reset-all",
    response_model=SettingResetOut,
    summary="review #9 · 一键恢复全部默认值",
)
def reset_all_settings(db: Session = Depends(get_db)) -> SettingResetOut:
    deleted = settings_service.reset_all(db)
    return SettingResetOut(reset=deleted, settings=_snapshot(db))


# ---------------------------------------------------------------------------
# 启动自检：发现 RUNTIME_KEYS 里有缺描述 / 没归组的 key 立刻报警（不阻断启动）
# ---------------------------------------------------------------------------


def _self_check() -> None:
    missing_desc = sorted(RUNTIME_KEYS.keys() - _DESCRIPTIONS.keys())
    missing_group = sorted(RUNTIME_KEYS.keys() - _KEY_TO_GROUP.keys())
    if missing_desc:
        logger.warning("settings: 以下 key 缺描述（GET 会返回空字符串）：%s", missing_desc)
    if missing_group:
        logger.warning("settings: 以下 key 未归组（GET 会归到 misc）：%s", missing_group)


_self_check()

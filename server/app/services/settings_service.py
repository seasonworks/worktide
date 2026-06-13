"""运行期可配置项服务：DB 键值覆盖 + .env/默认回退 + 进程内缓存。

业务逻辑（idle/offline 判定、break 上限等）一律经此读取，不硬编码。
DB 中无对应行时回退到 pydantic Settings（即 .env / 代码默认）。
"""
import logging
import threading

from sqlalchemy import select

from ..config import settings
from ..database import SessionLocal
from ..models import Setting

logger = logging.getLogger(__name__)

# 运行期可配置键 -> 类型。默认值取自 pydantic Settings（.env 种子 / 回退）。
# Phase 6.5A · 加入 9 个 GS-* 键；类型沿用现有 _parse 支持的 int/bool/float/str。
# "时分字符串"如 "09:00" 用 str 存储，正则校验放在 routers/settings.py 写入层。
RUNTIME_KEYS: dict[str, type] = {
    # 原有 6 项（保持兼容；offline_threshold_seconds / break_pauses_detection 暂不进 UI 但 API 可改）
    "idle_threshold_seconds": int,
    "offline_threshold_seconds": int,
    "break_meal_max_seconds": int,
    "break_toilet_max_seconds": int,
    "break_smoke_max_seconds": int,
    "break_pauses_detection": bool,
    "auto_return_after_break_timeout": bool,      # GS-13 · 超时是否自动回座
    # Phase 6.5A 新增 9 项
    "notify_break_timeout_enabled": bool,         # GS-3
    "notify_break_switch_enabled": bool,          # GS-7B
    "notify_idle_enter_enabled": bool,            # GS-4
    "notify_idle_exit_enabled": bool,             # GS-5
    "notify_clock_in_reminder_enabled": bool,     # GS-6
    "notify_clock_out_reminder_enabled": bool,    # GS-7
    "clock_in_reminder_time": str,                # "HH:MM"
    "clock_out_reminder_time": str,               # "HH:MM"
    "reminders_timezone_offset_hours": int,       # 中国 +8
    # 6.5B 新增
    "clock_reminder_mode": str,                   # GS-11 group / individual
    "pre_clock_in_minutes": int,                  # GS-11 上班提醒提前量（分钟）
    "admin_telegram_user_ids": str,               # Mention 管理员 user_id 列表（逗号分隔）
}


def _parse(raw, typ):
    if typ is bool:
        return str(raw).strip().lower() in ("1", "true", "yes", "on")
    if typ is int:
        return int(raw)
    if typ is float:
        return float(raw)
    return str(raw)


class SettingsService:
    """线程安全的设置读取/更新。读多写少，缓存全部 DB 覆盖，更新后失效。"""

    def __init__(self) -> None:
        self._cache: dict[str, str] = {}
        self._loaded = False
        self._lock = threading.Lock()

    def _ensure_loaded(self) -> None:
        if self._loaded:
            return
        with self._lock:
            if self._loaded:
                return
            db = SessionLocal()
            try:
                rows = db.scalars(select(Setting)).all()
                self._cache = {r.key: r.value for r in rows}
                self._loaded = True
                logger.info("settings_service 已加载 %d 项 DB 覆盖", len(self._cache))
            except Exception:  # noqa: BLE001  加载失败不应阻断启动，退化为默认值
                logger.exception("加载 settings 失败，暂用默认值")
            finally:
                db.close()

    def invalidate(self) -> None:
        with self._lock:
            self._loaded = False
            self._cache = {}

    def get(self, key: str):
        """取设置值：DB 覆盖优先，否则回退 .env/默认；按 RUNTIME_KEYS 类型转换。"""
        if key not in RUNTIME_KEYS:
            raise KeyError(f"未知设置项: {key}")
        self._ensure_loaded()
        typ = RUNTIME_KEYS[key]
        if key in self._cache:
            try:
                return _parse(self._cache[key], typ)
            except (ValueError, TypeError):
                logger.warning("设置 %s 值非法(%r)，回退默认", key, self._cache[key])
        return getattr(settings, key)

    def get_break_max(self, break_type) -> int:
        """按 break 类型取最大时长（秒）。"""
        from ..constants import BREAK_TYPE_TO_MAX_KEY, BreakType

        bt = break_type if isinstance(break_type, BreakType) else BreakType(break_type)
        return int(self.get(BREAK_TYPE_TO_MAX_KEY[bt]))

    def as_dict(self) -> dict:
        """合并默认 + DB 覆盖，供 /settings 接口展示。"""
        return {key: self.get(key) for key in RUNTIME_KEYS}

    def update(self, db, key: str, value) -> object:
        """更新一项设置（校验类型 → 写 DB → 失效缓存）。返回解析后的值。"""
        if key not in RUNTIME_KEYS:
            raise KeyError(f"未知设置项: {key}")
        typ = RUNTIME_KEYS[key]
        parsed = _parse(value, typ)  # 不可解析则抛错，由调用方处理

        row = db.get(Setting, key)
        if row is None:
            row = Setting(key=key, value=str(value), value_type=typ.__name__)
            db.add(row)
        else:
            row.value = str(value)
            row.value_type = typ.__name__
        db.commit()
        self.invalidate()
        logger.info("设置已更新 %s=%s", key, value)
        return parsed

    def reset(self, db, key: str) -> object:
        """Phase 6.5A · 重置为默认：删 settings 表那一行，让 get() 回退到 pydantic Settings。
        若行本来就不存在则 no-op；都会刷新缓存。返回回退后的值（供 UI 立即展示）。
        """
        if key not in RUNTIME_KEYS:
            raise KeyError(f"未知设置项: {key}")
        row = db.get(Setting, key)
        if row is not None:
            db.delete(row)
            db.commit()
        self.invalidate()
        default = getattr(settings, key)
        logger.info("设置已重置为默认 %s=%s", key, default)
        return default

    def reset_all(self, db) -> int:
        """重置所有运行期 key 到默认值（review #9 一键恢复）。返回实际删除的行数。"""
        deleted = 0
        for key in RUNTIME_KEYS:
            row = db.get(Setting, key)
            if row is not None:
                db.delete(row)
                deleted += 1
        if deleted:
            db.commit()
        self.invalidate()
        logger.info("已重置 %d 项设置为默认", deleted)
        return deleted

    def default_of(self, key: str):
        """返回某 key 的回退默认值（pydantic Settings 字段值）。"""
        if key not in RUNTIME_KEYS:
            raise KeyError(f"未知设置项: {key}")
        return getattr(settings, key)


settings_service = SettingsService()

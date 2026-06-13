"""Phase 6.5A · Telegram 通知器（统一入口）。

历史背景：
    本模块在 Phase 5 时只有 `notify_idle()` 单中文挂机告警；6.5A 重构为统一入口，
    覆盖 6 种业务事件 × 双语模板 × 独立 cooldown × runtime 开关（settings 表）。

设计取舍（继续遵循"不过度架构"）：
- 模板用 f-string 函数 + 双语模板表，inline 在本文件，方便运维一键改文案
- 时间统一 UTC+8（员工与管理员都在中国，UTC 不直观）
- cooldown 仍走内存 dict（同 5.x 决策），按 (notif_type, employee_id) 二维分桶，
  类型之间互不串扰；进程重启会清空（可接受，至多多发一次）
- 类型 enabled 开关来自 settings_service（runtime 可改），与 `telegram_enabled`
  (env 级总闸) 是 AND 关系
- dry_run=True 路径：跳过 enabled / cooldown / 发送，只返回模板字符串。**专给
  smoke_notifier.py 用，生产路径永远是 dry_run=False**

向后兼容：
    `notify_idle` 保留为 `notify_idle_enter` 的别名（活在 activity.py:62），Sprint C
    切到新名后可删，**但不要在 Sprint B 删**，否则会破坏现行挂机告警。
"""
from __future__ import annotations

import html as _html
import json
import logging
import threading
import time
import urllib.error
import urllib.request
from datetime import datetime, timedelta, timezone

from sqlalchemy import select

from .config import settings
from .database import SessionLocal
from .models import TelegramUser
from .services.settings_service import settings_service

logger = logging.getLogger(__name__)

_TELEGRAM_API = "https://api.telegram.org/bot{token}/sendMessage"

# 员工与管理员都在中国时区；模板里的"时间："统一这个 TZ
_TZ_LOCAL = timezone(timedelta(hours=8))

# ---------------------------------------------------------------------------
# 通知类型枚举（cooldown / settings 开关 / 路由 都靠这个 key）
# ---------------------------------------------------------------------------


class NotifyType:
    BREAK_TIMEOUT = "break_timeout"
    BREAK_SWITCH = "break_switch"
    IDLE_ENTER = "idle_enter"
    IDLE_EXIT = "idle_exit"
    CLOCK_IN_REMINDER = "clock_in_reminder"
    CLOCK_OUT_REMINDER = "clock_out_reminder"


# 每类型独立的 cooldown 秒数。0 = 不抑制（每次都发）
# 数值来自 config.py（review 已确认 idle_exit=600；其他参考既有 telegram_cooldown）
def _cooldown_for(notif_type: str) -> int:
    if notif_type == NotifyType.BREAK_TIMEOUT:
        return 0                                                # 每次超时都通知
    if notif_type == NotifyType.BREAK_SWITCH:
        return 0                                                # 每次切换都通知
    if notif_type == NotifyType.IDLE_ENTER:
        return 0                                                # 频控改由 activity 层 bucket
        # （每个 idle_threshold 段最多发一次：15/30/45 min…），此处不再用时间冷却
    if notif_type == NotifyType.IDLE_EXIT:
        return settings.notify_idle_exit_cooldown_seconds       # 锁定 10 min (review #3)
    if notif_type == NotifyType.CLOCK_IN_REMINDER:
        return settings.telegram_cooldown_seconds               # 30 min 防重提醒
    if notif_type == NotifyType.CLOCK_OUT_REMINDER:
        return settings.telegram_cooldown_seconds
    return 0


# 类型 -> RUNTIME_KEYS 中的开关 key（None = 不受 runtime 开关控制）
_TYPE_TO_ENABLED_KEY: dict[str, str] = {
    NotifyType.BREAK_TIMEOUT:      "notify_break_timeout_enabled",
    NotifyType.BREAK_SWITCH:       "notify_break_switch_enabled",
    NotifyType.IDLE_ENTER:         "notify_idle_enter_enabled",
    NotifyType.IDLE_EXIT:          "notify_idle_exit_enabled",
    NotifyType.CLOCK_IN_REMINDER:  "notify_clock_in_reminder_enabled",
    NotifyType.CLOCK_OUT_REMINDER: "notify_clock_out_reminder_enabled",
}


# ---------------------------------------------------------------------------
# Break type 中英文映射（DB 存 "smoke"/"meal"/"toilet"；UI 要双语展示）
# ---------------------------------------------------------------------------

_BREAK_LABEL: dict[str, tuple[str, str]] = {
    "smoke":  ("Smoke Break",  "抽烟"),
    "meal":   ("Lunch Break",  "吃饭"),
    "toilet": ("Restroom Break", "上厕所"),
}


def _break_label(break_type: str) -> tuple[str, str]:
    """未知 break_type 兜底原样返回，不抛异常。"""
    return _BREAK_LABEL.get(break_type, (break_type, break_type))


# ---------------------------------------------------------------------------
# 双语模板（review 要求 · 员工姓名第一行 · UTC+8 时间统一）
# 每个 build_* 返回**完整 Telegram 消息文本**，可直接进 _send()
# 修改模板只需改这一处；不要把模板拆到别的文件，否则运维难找
# ---------------------------------------------------------------------------


def _now_str() -> str:
    return datetime.now(_TZ_LOCAL).strftime("%Y-%m-%d %H:%M")


def _fmt_minutes(seconds: int) -> str:
    m = max(0, seconds) // 60
    return f"{m} minutes / {m} 分钟"


def build_break_timeout(
    employee_name: str,
    break_type: str,
    duration_seconds: int,
    auto_returned: bool = True,
) -> str:
    """GS-3 / GS-13 · 休息超时通知。

    auto_returned=True（默认 · 自动回座 ON）：
        提示"系统已自动返回工作岗位"
    auto_returned=False（GS-13 自动回座 OFF）：
        提示"休息时间已超过设定限制 / 请员工尽快返回工作岗位"，员工需手动回座
    """
    en, zh = _break_label(break_type)
    if auto_returned:
        body = (
            "System automatically returned employee to work\n"
            "系统已自动返回工作岗位\n"
        )
    else:
        body = (
            "Break duration exceeded configured limit\n"
            "休息时间已超过设定限制\n"
            "\n"
            "Employee action required\n"
            "请员工尽快返回工作岗位\n"
        )
    return (
        "⏰ Break Timeout / 休息超时\n"
        "\n"
        f"员工 / Employee:\n"
        f"{employee_name}\n"
        "\n"
        f"休息类型 / Break Type:\n"
        f"{en} / {zh}\n"
        "\n"
        f"持续时间 / Duration:\n"
        f"{_fmt_minutes(duration_seconds)}\n"
        "\n"
        f"{body}"
        "\n"
        f"时间 / Time:\n"
        f"{_now_str()}"
    )


def build_break_switch(employee_name: str, from_break: str, to_break: str) -> str:
    f_en, f_zh = _break_label(from_break)
    t_en, t_zh = _break_label(to_break)
    return (
        "🔄 Break Switched / 切换休息\n"
        "\n"
        f"员工 / Employee:\n"
        f"{employee_name}\n"
        "\n"
        f"切换 / Switched:\n"
        f"{f_en} → {t_en}\n"
        f"{f_zh} → {t_zh}\n"
        "\n"
        f"时间 / Time:\n"
        f"{_now_str()}"
    )


def build_idle_enter(employee_name: str, idle_seconds: int) -> str:
    return (
        f"🟡 员工 / Employee:\n"
        f"{employee_name}\n"
        "\n"
        "Current Status: Idle\n"
        "当前状态：空闲挂机\n"
        "\n"
        f"连续无操作 / Idle Duration:\n"
        f"{_fmt_minutes(idle_seconds)}\n"
        "\n"
        f"时间 / Time:\n"
        f"{_now_str()}"
    )


def build_idle_exit(employee_name: str, idle_duration_seconds: int) -> str:
    return (
        f"🟢 员工 / Employee:\n"
        f"{employee_name}\n"
        "\n"
        "Current Status: Active\n"
        "当前状态：恢复工作\n"
        "\n"
        f"挂机时长 / Idle Duration:\n"
        f"{_fmt_minutes(idle_duration_seconds)}\n"
        "\n"
        f"时间 / Time:\n"
        f"{_now_str()}"
    )


def build_clock_in_reminder(employee_name: str) -> str:
    return (
        "⏰ Clock-In Reminder / 上班提醒\n"
        "\n"
        f"员工 / Employee:\n"
        f"{employee_name}\n"
        "\n"
        "Please clock in.\n"
        "请及时上班打卡。\n"
        "\n"
        f"时间 / Time:\n"
        f"{_now_str()}"
    )


def build_clock_out_reminder(employee_name: str) -> str:
    return (
        "🏁 Clock-Out Reminder / 下班提醒\n"
        "\n"
        f"员工 / Employee:\n"
        f"{employee_name}\n"
        "\n"
        "Please clock out.\n"
        "请及时下班打卡。\n"
        "\n"
        f"时间 / Time:\n"
        f"{_now_str()}"
    )


def build_clock_in_group_reminder(pre_minutes: int) -> str:
    """GS-11 · 群发上班提醒（不 @ 个人；上班前 pre_minutes 分钟发一次）。"""
    return (
        "⏰ 上班提醒 / Start Work Reminder\n"
        "\n"
        f"距离上班时间还有 {pre_minutes} 分钟，\n"
        "请准备开始工作并及时打卡。\n"
        "Work starts soon — please get ready and clock in on time.\n"
        "\n"
        f"时间 / Time:\n"
        f"{_now_str()}"
    )


def build_clock_out_group_reminder() -> str:
    """GS-11 · 群发下班提醒（不 @ 个人；下班时刻发一次）。"""
    return (
        "🏁 下班提醒 / End Work Reminder\n"
        "\n"
        "已到下班时间，\n"
        "请及时下班打卡。\n"
        "End of workday — please clock out.\n"
        "\n"
        f"时间 / Time:\n"
        f"{_now_str()}"
    )


# ---------------------------------------------------------------------------
# 6.5B · Mention（@员工 / @管理员）
# - user_id 优先（2026-06 改）：HTML 内联链接 <a href="tg://user?id=ID">名</a>（需 parse_mode=HTML）。
#   user_id 不可变，员工改了用户名也 @ 得到；旧式 @username 会因改名失效。
# - 无 user_id 仅 username：退回纯文本 "@username"（Telegram 自动高亮）
# - 都没有：降级为纯文本名字（不构成 mention，不丢通知）
# 规则：Idle Enter=@员工+@管理员；Break Timeout=@员工；其它不 @
# ---------------------------------------------------------------------------


def _resolve_employee_mention(employee_id: int) -> tuple[str | None, int | None]:
    """查 telegram_users 取 (username, user_id)；未绑定 / 异常 → (None, None)。
    自建只读短事务，失败降级，绝不向上抛（不影响通知发送）。"""
    try:
        db = SessionLocal()
        try:
            row = db.scalar(
                select(TelegramUser).where(TelegramUser.employee_id == employee_id)
            )
            if row is None:
                return None, None
            return row.telegram_username, row.telegram_user_id
        finally:
            db.close()
    except Exception:  # noqa: BLE001
        logger.exception("解析员工 %s 的 Telegram mention 失败，降级纯文本", employee_id)
        return None, None


def _admin_user_ids() -> list[int]:
    """从 settings 读管理员 user_id 列表（逗号/分号分隔）。非法项跳过。"""
    raw = ""
    try:
        raw = str(settings_service.get("admin_telegram_user_ids") or "")
    except Exception:  # noqa: BLE001
        logger.exception("读取 admin_telegram_user_ids 失败")
        return []
    ids: list[int] = []
    for part in raw.replace(";", ",").split(","):
        part = part.strip()
        if not part:
            continue
        try:
            ids.append(int(part))
        except ValueError:
            logger.warning("admin_telegram_user_ids 含非法 id: %r", part)
    return ids


def _build_mention_line(targets: list[tuple[str, str | None, int | None]]) -> tuple[str, str, bool]:
    """targets: [(display, username, user_id), ...]
    返回 (html_line, plain_line, needs_html)。

    优先级（2026-06 改：数字 id 优先）：
    - 有 user_id → HTML 内联链接 tg://user?id=（needs_html=True）；plain 版仅显示名字。
      user_id 不可变，员工即便改了 Telegram 用户名也照样 @ 得到（旧 @username 会失效，
      甚至 @ 到抢注旧名的陌生人）。
    - 无 user_id 仅 username → 退回 @username（两版一致，无需 HTML）。
    - 都无 → 纯文本名字（非 mention）。
    """
    html_toks: list[str] = []
    plain_toks: list[str] = []
    needs_html = False
    for display, username, uid in targets:
        if uid:
            html_toks.append(f'<a href="tg://user?id={int(uid)}">{_html.escape(display)}</a>')
            plain_toks.append(display)
            needs_html = True
        elif username:
            tok = f"@{username}"
            html_toks.append(tok)
            plain_toks.append(tok)
        else:
            # 无 username 也无 user_id：无法构成 mention → 跳过，不前缀裸名字
            # （未绑定员工的通知保持原文，避免多一行冗余名字）
            continue
    return " ".join(html_toks), " ".join(plain_toks), needs_html


# 模板路由表，供 smoke 脚本 / 未来 admin 预览 接口用
_BUILDERS: dict[str, callable] = {
    NotifyType.BREAK_TIMEOUT:      build_break_timeout,
    NotifyType.BREAK_SWITCH:       build_break_switch,
    NotifyType.IDLE_ENTER:         build_idle_enter,
    NotifyType.IDLE_EXIT:          build_idle_exit,
    NotifyType.CLOCK_IN_REMINDER:  build_clock_in_reminder,
    NotifyType.CLOCK_OUT_REMINDER: build_clock_out_reminder,
}


# ---------------------------------------------------------------------------
# TelegramNotifier
# ---------------------------------------------------------------------------


class TelegramNotifier:
    """统一 Telegram 通知入口。

    线程安全：所有写 cooldown dict 的路径在 _lock 内；模板构造和发送在锁外
    （避免 urllib.urlopen 期间持锁）。
    """

    def __init__(self) -> None:
        # (notif_type, employee_id) -> 上次发送时刻（time.monotonic）
        self._cooldown: dict[tuple[str, int], float] = {}
        self._lock = threading.Lock()

    # ----- 通用 gate -----
    @property
    def configured(self) -> bool:
        """env 级总闸：telegram_enabled + token + chat_id 三者都有。"""
        return bool(
            settings.telegram_enabled
            and settings.telegram_bot_token
            and settings.telegram_chat_id
        )

    def _enabled(self, notif_type: str) -> bool:
        """env 总闸 AND runtime 子开关；只要其一关，整条通知不发。"""
        if not self.configured:
            return False
        runtime_key = _TYPE_TO_ENABLED_KEY.get(notif_type)
        if runtime_key is None:
            return True
        try:
            return bool(settings_service.get(runtime_key))
        except Exception:  # noqa: BLE001  settings_service 异常不应阻断
            logger.exception("读取 setting %s 失败，按 enabled 处理", runtime_key)
            return True

    def _claim_cooldown(self, notif_type: str, employee_id: int) -> bool:
        """冷却期内返回 False；否则占用并返回 True。cooldown=0 则永远 True。"""
        cd = _cooldown_for(notif_type)
        if cd <= 0:
            return True
        now = time.monotonic()
        key = (notif_type, employee_id)
        with self._lock:
            last = self._cooldown.get(key)
            if last is not None and (now - last) < cd:
                return False
            self._cooldown[key] = now
            return True

    def reset_cooldowns(self) -> None:
        """供运维 / 测试用：清空全部 cooldown 状态。"""
        with self._lock:
            self._cooldown.clear()

    # ----- 内部发送 -----
    def _send(self, text: str, parse_mode: str | None = None,
              fallback_text: str | None = None) -> None:
        """发送一条通知。

        6.5B：支持 parse_mode（用于 HTML mention 链接）。若带 parse_mode 的发送因
        HTTP 4xx（最常见是 entity 解析 400）失败，且提供了 fallback_text，则**降级为
        纯文本重发一次**，保证通知不丢失（满足"parse_mode 异常自动降级 / 不允许丢失"）。
        """
        url = _TELEGRAM_API.format(token=settings.telegram_bot_token)
        body = {"chat_id": settings.telegram_chat_id, "text": text}
        if parse_mode:
            body["parse_mode"] = parse_mode
        payload = json.dumps(body).encode("utf-8")
        req = urllib.request.Request(
            url, data=payload, headers={"Content-Type": "application/json"}
        )
        try:
            with urllib.request.urlopen(
                req, timeout=settings.telegram_timeout_seconds
            ) as resp:
                resp.read()
            head = text.split("\n", 1)[0]
            logger.info("已发送 Telegram 通知：%s", head)
        except urllib.error.HTTPError as exc:
            # parse_mode 解析失败（400）等 → 降级纯文本重发，避免通知丢失
            if parse_mode is not None and fallback_text is not None:
                logger.warning(
                    "带 parse_mode=%s 的通知发送失败(%s)，降级纯文本重发", parse_mode, exc
                )
                self._send(fallback_text)  # 纯文本，无 parse_mode / fallback
            else:
                logger.warning("发送 Telegram 通知失败：%s", exc)
        except urllib.error.URLError as exc:
            # 网络错误：纯文本重试通常也会失败，但仍尽力一次（不丢通知优先）
            if parse_mode is not None and fallback_text is not None:
                logger.warning("通知发送失败(%s)，降级纯文本重试", exc)
                self._send(fallback_text)
            else:
                logger.warning("发送 Telegram 通知失败：%s", exc)
        except Exception:  # noqa: BLE001  通知失败不应影响主流程
            logger.exception("发送 Telegram 通知异常")

    # ----- 通用 dispatch（所有 notify_* 走它） -----
    def _dispatch(
        self,
        notif_type: str,
        employee_id: int,
        text: str,
        dry_run: bool = False,
    ) -> str | None:
        """统一处理：dry_run / enabled / cooldown / 发送。

        返回值：
            - dry_run=True 永远返回 text（不查 enabled / cooldown，纯模板预览）
            - 否则返回发送出去的 text，或 None（被开关或冷却拦截）
        """
        if dry_run:
            logger.debug("[notifier dry-run] %s emp=%s", notif_type, employee_id)
            return text
        if not self._enabled(notif_type):
            logger.debug("通知类型 %s 未启用或 Telegram 未配置，跳过", notif_type)
            return None
        if not self._claim_cooldown(notif_type, employee_id):
            logger.info("emp=%s 类型 %s 仍在冷却内，跳过", employee_id, notif_type)
            return None
        self._send(text)
        return text

    def _dispatch_with_mentions(
        self,
        notif_type: str,
        employee_id: int,
        base_text: str,
        targets: list[tuple[str, str | None, int | None]],
        dry_run: bool = False,
    ) -> str | None:
        """6.5B · 带 @mention 的 dispatch。

        在 base_text 顶部加一行 mention。若任一目标只能用 tg://user?id=（无 username），
        则以 HTML 发送并在失败时降级纯文本（_send 内部处理）。
        dry_run 返回纯文本预览。
        """
        html_line, plain_line, needs_html = _build_mention_line(targets)
        plain_text = (plain_line + "\n" + base_text) if plain_line else base_text

        if dry_run:
            logger.debug("[notifier dry-run] %s emp=%s (mention)", notif_type, employee_id)
            return plain_text
        if not self._enabled(notif_type):
            logger.debug("通知类型 %s 未启用或 Telegram 未配置，跳过", notif_type)
            return None
        if not self._claim_cooldown(notif_type, employee_id):
            logger.info("emp=%s 类型 %s 仍在冷却内，跳过", employee_id, notif_type)
            return None

        if needs_html:
            html_text = html_line + "\n" + _html.escape(base_text)
            self._send(html_text, parse_mode="HTML", fallback_text=plain_text)
        else:
            self._send(plain_text)
        return plain_text

    # ----- 公开 notify_* API -----

    def notify_break_timeout(
        self,
        employee_id: int,
        employee_name: str,
        break_type: str,
        duration_seconds: int,
        auto_returned: bool = True,
        dry_run: bool = False,
    ) -> str | None:
        """GS-3 / GS-13 · sweeper 检测到超时 break 后调用。

        auto_returned=True：自动回座模式（sweeper 已结束 break 并恢复 working）
        auto_returned=False：保持休息模式（仅提醒，员工需手动回座）
        两种模式共用 notify_break_timeout_enabled 开关与 cooldown=0。
        6.5B Mention：@员工（不 @管理员）。
        """
        text = build_break_timeout(
            employee_name, break_type, duration_seconds, auto_returned=auto_returned
        )
        username, uid = _resolve_employee_mention(employee_id)
        targets = [(employee_name, username, uid)]
        return self._dispatch_with_mentions(
            NotifyType.BREAK_TIMEOUT, employee_id, text, targets, dry_run=dry_run
        )

    def notify_break_switch(
        self,
        employee_id: int,
        employee_name: str,
        from_break: str,
        to_break: str,
        dry_run: bool = False,
    ) -> str | None:
        """GS-7B · work_state.start_break 在 switched 分支调用。"""
        text = build_break_switch(employee_name, from_break, to_break)
        return self._dispatch(NotifyType.BREAK_SWITCH, employee_id, text, dry_run=dry_run)

    def notify_idle_enter(
        self,
        employee_id: int,
        employee_name: str,
        idle_seconds: int,
        dry_run: bool = False,
    ) -> str | None:
        """GS-4 · activity.record_report 在 prev=online→curr=idle 时调用。
        6.5B Mention：@员工 + @管理员（仅挂机进入事件 @管理员）。"""
        text = build_idle_enter(employee_name, idle_seconds)
        username, uid = _resolve_employee_mention(employee_id)
        targets: list[tuple[str, str | None, int | None]] = [(employee_name, username, uid)]
        for admin_id in _admin_user_ids():
            targets.append(("管理员 / Admin", None, admin_id))
        return self._dispatch_with_mentions(
            NotifyType.IDLE_ENTER, employee_id, text, targets, dry_run=dry_run
        )

    def notify_idle_exit(
        self,
        employee_id: int,
        employee_name: str,
        idle_duration_seconds: int,
        dry_run: bool = False,
    ) -> str | None:
        """GS-5 · activity.record_report 在 prev=idle→curr=online 时调用。"""
        text = build_idle_exit(employee_name, idle_duration_seconds)
        return self._dispatch(NotifyType.IDLE_EXIT, employee_id, text, dry_run=dry_run)

    def notify_clock_in_reminder(
        self,
        employee_id: int,
        employee_name: str,
        dry_run: bool = False,
    ) -> str | None:
        """GS-6 · clock_reminder_scheduler 在配置时刻调用。"""
        text = build_clock_in_reminder(employee_name)
        return self._dispatch(NotifyType.CLOCK_IN_REMINDER, employee_id, text, dry_run=dry_run)

    def notify_clock_out_reminder(
        self,
        employee_id: int,
        employee_name: str,
        dry_run: bool = False,
    ) -> str | None:
        """GS-7 · clock_reminder_scheduler 在配置时刻调用。"""
        text = build_clock_out_reminder(employee_name)
        return self._dispatch(NotifyType.CLOCK_OUT_REMINDER, employee_id, text, dry_run=dry_run)

    def notify_clock_in_group_reminder(
        self, pre_minutes: int, dry_run: bool = False
    ) -> str | None:
        """GS-11 · 群发上班提醒（不 @ 个人）。employee_id=0 占位（群发无具体员工）。
        共用 notify_clock_in_reminder_enabled 开关；每日幂等由 scheduler 保证。"""
        text = build_clock_in_group_reminder(pre_minutes)
        return self._dispatch(NotifyType.CLOCK_IN_REMINDER, 0, text, dry_run=dry_run)

    def notify_clock_out_group_reminder(self, dry_run: bool = False) -> str | None:
        """GS-11 · 群发下班提醒（不 @ 个人）。"""
        text = build_clock_out_group_reminder()
        return self._dispatch(NotifyType.CLOCK_OUT_REMINDER, 0, text, dry_run=dry_run)

    # Phase 6.5A Sprint C 已把 activity.py 切到 notify_idle_enter；
    # Sprint E 移除 notify_idle 兼容别名。如线上发现还有调用点，恢复 4 行别名即可：
    #     def notify_idle(self, employee_id, employee_name, idle_seconds):
    #         self.notify_idle_enter(employee_id, employee_name, idle_seconds)


# 进程内单例：cooldown 状态需跨请求共享
notifier = TelegramNotifier()

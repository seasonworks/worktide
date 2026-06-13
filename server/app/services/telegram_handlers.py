"""Telegram 更新派发（纯逻辑层，不联网）。

`handle_update(db, update)` 解析 message/callback_query，按 telegram_user_id **实时查库**
鉴权（不缓存），调用 work_state 状态机，返回"发送意图"列表（SendMessage / AnswerCallback）
供 poller 执行。便于脱网单测。

约束：
- 未绑定用户任何交互 → 记 seen + 拒绝提示。
- callback 永远返回一个 AnswerCallback（即便动作异常），保证客户端不一直转圈。
"""
import logging
from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone

from sqlalchemy.orm import Session

from ..models import Employee
from ..notifier import notifier
from . import manual_punch, punch_debounce, telegram_bindings, work_state
from .settings_service import settings_service

logger = logging.getLogger(__name__)


def _on_break_switch(employee_id: int, employee_name: str,
                      from_break: str, to_break: str) -> None:
    """work_state.start_break 的 on_switch 回调（Phase 6.5A · GS-7B）。
    单独命名便于 traceback + smoke 用 inspect.getsource 验证存在。
    notifier 内部已检查 notify_break_switch_enabled 开关。"""
    notifier.notify_break_switch(
        employee_id=employee_id,
        employee_name=employee_name,
        from_break=from_break,
        to_break=to_break,
    )

_UNBOUND_HINT = "你尚未绑定员工，请联系管理员绑定后使用。"
_INACTIVE_HINT = "员工已离职，无法打卡"
_ERROR_HINT = "操作失败，请稍后重试"

# Phase 6.0E-B · 群组 UX：所有 callback toast 双语化
# Telegram AnswerCallback 显示限制 ~200 字符；下行短文本双语 1-2 行
_TOAST_UNBOUND  = "❌ Not Bound\n尚未绑定员工"
_TOAST_INACTIVE = "❌ Employee Inactive\n员工已离职"
_TOAST_FAIL     = "❌ Operation Failed\n操作失败"

# GS-12 · Action Feedback Improvement
# 状态机 noop（ActionResult.changed=False）不是错误，给友好双语提示而非 _TOAST_FAIL。
# 真正异常（DB/服务/Telegram/写入失败）会在 _handle_* 的 try/except 里抛出 →
# 仍走 _TOAST_FAIL，与此处互不冲突。
# key = ActionResult.code（见 work_state.py 各动作返回值）
_NOOP_TOAST: dict[str, str] = {
    "already_on_shift":  "ℹ️ Already Working\n当前已处于上班状态。",
    "already_off_shift": "ℹ️ Already Off Duty\n当前已处于下班状态。",
    "not_on_break":      "ℹ️ Already Working\n当前已处于工作状态，无需回座。",
    "already_returned":  "ℹ️ Already Working\n当前已处于工作状态，无需回座。",
    "not_on_shift":      "ℹ️ Not On Shift\n请先上班打卡再开始休息。",
}

# break_already_active 同一个 code 对应三种休息类型，按 callback_data 细分文案
_BREAK_ACTIVE_TOAST: dict[str, str] = {
    "ws:break:meal":   "ℹ️ Already On Lunch Break\n当前已处于用餐状态。",
    "ws:break:smoke":  "ℹ️ Already On Smoke Break\n当前已处于抽烟状态。",
    "ws:break:toilet": "ℹ️ Already On Restroom Break\n当前已处于如厕状态。",
}


def _noop_feedback(result, callback_data: str) -> str:
    """GS-12 · 把状态机 noop（changed=False）翻译成友好双语提示。

    - result is None（未识别的 callback_data）：非状态机结果，保持 _TOAST_FAIL
    - break_already_active：按 callback_data 区分用餐/抽烟/如厕
    - 已知 code：查 _NOOP_TOAST
    - 其它 noop code：兜底用状态机中文 message（本就友好），加 ℹ️ 前缀
    """
    if result is None:
        return _TOAST_FAIL
    code = getattr(result, "code", "") or ""
    if code == "break_already_active":
        return _BREAK_ACTIVE_TOAST.get(
            callback_data, "ℹ️ Already On Break\n当前已在休息中。"
        )
    if code in _NOOP_TOAST:
        return _NOOP_TOAST[code]
    msg = getattr(result, "message", "") or "无需重复操作"
    return f"ℹ️ {msg}"

# Phase 5.6A: 极简 /help（不查 DB；未绑定用户也能调用，用于探活）
_HELP_TEXT = (
    "Worktide 打卡助手\n"
    "/start  打开打卡面板（需先绑定员工）\n"
    "/menu   同 /start\n"
    "/help   显示本帮助\n"
    "未绑定用户请联系管理员通过后台绑定。"
)

# callback_data 命名空间：ws:clock_in / ws:clock_out / ws:back / ws:break:<type>
_ACTION_PREFIX = "ws:"

# Phase 6.0E-B · 群组打卡反馈完整配置表
# 每条 callback_data 给四样东西：
#   toast_en / toast_zh           — 短双语 toast（AnswerCallback，~1s 消失）
#   status_emoji + status_en/zh   — 永久聊天消息顶部"当前状态"双语
#   op_en / op_zh                 — 永久聊天消息里"操作"双语标签
# 仅当 ActionResult.changed=True 时这俩才一起发；changed=False 走 _TOAST_FAIL
_ACTION_TABLE: dict[str, dict[str, str]] = {
    "ws:clock_in": {
        "toast_en":     "✅ Clock In Successful",
        "toast_zh":     "上班打卡成功",
        "status_emoji": "🟢",
        "status_en":    "Working",
        "status_zh":    "工作中",
        "op_en":        "Clock In",
        "op_zh":        "上班",
    },
    "ws:clock_out": {
        "toast_en":     "🏁 Workday Ended",
        "toast_zh":     "下班打卡成功",
        "status_emoji": "⚫",
        "status_en":    "Off Duty",
        "status_zh":    "已下班",
        "op_en":        "End Work",
        "op_zh":        "下班",
    },
    "ws:back": {
        "toast_en":     "✅ Back To Work",
        "toast_zh":     "已返回工作岗位",
        "status_emoji": "🟢",
        "status_en":    "Working",
        "status_zh":    "工作中",
        "op_en":        "Back To Work",
        "op_zh":        "回座",
    },
    "ws:break:meal": {
        "toast_en":     "🍽 Lunch Break Started",
        "toast_zh":     "开始用餐",
        "status_emoji": "🍽",
        "status_en":    "Lunch Break",
        "status_zh":    "用餐中",
        "op_en":        "Lunch",
        "op_zh":        "吃饭",
    },
    "ws:break:toilet": {
        "toast_en":     "🚻 Restroom Break Started",
        "toast_zh":     "开始上厕所",
        "status_emoji": "🚻",
        "status_en":    "Restroom Break",
        "status_zh":    "如厕中",
        "op_en":        "Restroom",
        "op_zh":        "厕所",
    },
    "ws:break:smoke": {
        "toast_en":     "🚬 Smoke Break Started",
        "toast_zh":     "开始抽烟",
        "status_emoji": "🚬",
        "status_en":    "Smoke Break",
        "status_zh":    "抽烟中",
        "op_en":        "Smoke",
        "op_zh":        "抽烟",
    },
}

# 用 UTC+8 给时间戳（员工在中国/东八区，UTC 时间对工位用户不直观）
_TZ_LOCAL = timezone(timedelta(hours=8))


def _build_action_toast(callback_data: str) -> str | None:
    cfg = _ACTION_TABLE.get(callback_data)
    if cfg is None:
        return None
    return f"{cfg['toast_en']}\n{cfg['toast_zh']}"


def _fmt_late(minutes: int) -> str:
    """迟到时长双语格式：<60 分钟显示分钟，否则 X小时Y分钟。"""
    if minutes >= 60:
        h, m = divmod(minutes, 60)
        return f"{h}小时{m}分钟 / {h}h{m}m"
    return f"{minutes} 分钟 / {minutes} min"


def _late_clock_in_line(now: datetime | None = None) -> str | None:
    """Clock In 迟到行（晚于设置的上班时刻才返回，否则 None）。

    按 `reminders_timezone_offset_hours` 把当前 UTC 换成员工本地时间，与设置的上班
    时刻 `clock_in_reminder_time`(HH:MM) 比较。跨午夜班次（如 22:00）：本地时间比上班
    时刻早 12h 以上时，视为上班时刻在昨天。任何读取/解析异常都安全返回 None（不影响回执）。
    """
    try:
        shift_hhmm = str(settings_service.get("clock_in_reminder_time") or "")
        offset = int(settings_service.get("reminders_timezone_offset_hours"))
        sh, sm = (int(x) for x in shift_hhmm.split(":", 1))
    except (ValueError, TypeError, AttributeError):
        return None

    now_utc = now or datetime.now(timezone.utc)
    now_local = now_utc + timedelta(hours=offset)
    shift_start = now_local.replace(hour=sh, minute=sm, second=0, microsecond=0)
    late_seconds = (now_local - shift_start).total_seconds()
    if late_seconds < -12 * 3600:        # 跨午夜：上班时刻其实是昨天
        late_seconds += 24 * 3600
    if late_seconds < 60:                # 不足 1 分钟不算迟到
        return None
    return f"迟到 / Late:\n{_fmt_late(int(late_seconds // 60))}\n\n"


def _build_action_chat_message(callback_data: str, employee_name: str) -> str | None:
    """Phase 6.0E-B · 永久聊天消息（含员工姓名、当前状态、操作、时间）。

    格式（严格对齐用户规格）：
        🟢 员工：LILY
        \n
        Current Status: Working
        当前状态：工作中
        \n
        操作：
        Clock In / 上班
        \n
        [Clock In 且迟到时插入：迟到 / Late: \n X 分钟 / X min \n]
        时间：
        2026-06-02 08:35
    """
    cfg = _ACTION_TABLE.get(callback_data)
    if cfg is None:
        return None
    ts = datetime.now(_TZ_LOCAL).strftime("%Y-%m-%d %H:%M")
    name = employee_name or "(未命名)"
    # Clock In 时若晚于设置的上班时刻，插入迟到行（其它动作不显示）
    late_block = _late_clock_in_line() if callback_data == "ws:clock_in" else None
    return (
        f"{cfg['status_emoji']} 员工：{name}\n"
        f"\n"
        f"Current Status: {cfg['status_en']}\n"
        f"当前状态：{cfg['status_zh']}\n"
        f"\n"
        f"操作：\n"
        f"{cfg['op_en']} / {cfg['op_zh']}\n"
        f"\n"
        f"{late_block or ''}"
        f"时间：\n"
        f"{ts}"
    )


@dataclass
class SendMessage:
    chat_id: object
    text: str
    reply_markup: dict | None = None


@dataclass
class AnswerCallback:
    callback_query_id: str
    text: str
    show_alert: bool = False


def build_menu_keyboard() -> dict:
    """6 按钮打卡面板（Phase 6.0B：按钮文本双语 中文/English；
    callback_data 与布局保持原样，状态枚举不变）。

    Phase 6.0E-C 起 /start /menu 默认改发 build_reply_keyboard() 持久键盘；
    这里保留 InlineKeyboardMarkup 仅供向后兼容（旧群里已有的菜单消息不
    失效，依然能点）。
    """
    return {
        "inline_keyboard": [
            [
                {"text": "上班 / Start Work", "callback_data": "ws:clock_in"},
                {"text": "下班 / End Work", "callback_data": "ws:clock_out"},
            ],
            [
                {"text": "吃饭 / Lunch", "callback_data": "ws:break:meal"},
                {"text": "厕所 / Restroom", "callback_data": "ws:break:toilet"},
                {"text": "抽烟 / Smoke Break", "callback_data": "ws:break:smoke"},
            ],
            [{"text": "回座 / Back To Work", "callback_data": "ws:back"}],
        ]
    }


def build_reply_keyboard() -> dict:
    """Phase 6.0E-C · 群组持久 ReplyKeyboardMarkup。

    与 InlineKeyboard 的本质区别：按钮按下发出**普通文本消息**（与按钮
    label 完全相同），而不是 callback_query。这意味着：

      a) 在群组中必须关闭 BotFather Group Privacy，否则 bot 收不到非
         /command 的群消息 → 按钮无响应。
      b) Bot 的 _handle_message 需要把按钮文本匹配回 callback_data 然后
         走同一套 _dispatch_action 路径。

    布局 2/3/1 与 InlineKeyboard 完全一致；is_persistent + resize_keyboard
    让键盘常驻输入框上方，员工无需翻聊天找按钮。
    """
    return {
        "keyboard": [
            [
                {"text": "上班 / Start Work"},
                {"text": "下班 / End Work"},
            ],
            [
                {"text": "吃饭 / Lunch"},
                {"text": "上厕所 / Restroom"},
                {"text": "抽烟 / Smoke Break"},
            ],
            [
                {"text": "回座 / Back To Work"},
            ],
        ],
        "is_persistent": True,
        "resize_keyboard": True,
        # 不设 one_time_keyboard 与 selective：群里所有员工共享同一键盘，
        # 按完不消失 → 之后无需再 /start。
    }


# 按钮文本（必须与 build_reply_keyboard 的 text 字段逐字一致）→ callback_data
# /start 后 Reply Keyboard 按钮按下 → bot 收到这些文本之一 → 走 _dispatch_action
_REPLY_BUTTON_TO_ACTION: dict[str, str] = {
    "上班 / Start Work":   "ws:clock_in",
    "下班 / End Work":     "ws:clock_out",
    "吃饭 / Lunch":        "ws:break:meal",
    "上厕所 / Restroom":   "ws:break:toilet",
    "抽烟 / Smoke Break":  "ws:break:smoke",
    "回座 / Back To Work": "ws:back",
}

# Phase 6.0E-C · /start 后发的欢迎消息（双语；带 Reply Keyboard）
_START_GREETING = "打卡面板已启用\nPunch Panel Enabled"


def _safe_seen(db: Session, tg_id, username) -> None:
    if tg_id is None:
        return
    try:
        telegram_bindings.upsert_seen_user(db, tg_id, username)
    except Exception:  # noqa: BLE001  记录失败不应影响后续动作
        logger.exception("记录 seen 用户失败 tg=%s", tg_id)
        db.rollback()


def _dispatch_action(db: Session, employee_id: int, data: str):
    """callback_data → work_state 动作。返回 ActionResult 或 None（未识别）。

    Phase 6.5A · break 切换路径注入 GS-7B 回调，notifier 内部自查 enabled 开关。
    """
    if data == "ws:clock_in":
        return work_state.clock_in(db, employee_id)
    if data == "ws:clock_out":
        return work_state.clock_out(db, employee_id)
    if data == "ws:back":
        return work_state.return_to_work(db, employee_id)
    if data.startswith("ws:break:"):
        break_type = data.split(":", 2)[2]
        return work_state.start_break(
            db, employee_id, break_type,
            on_switch=_on_break_switch,   # GS-7B · notify_break_switch
        )
    return None


def _execute_punch_action(
    db: Session,
    employee_id: int,
    employee_name: str,
    callback_data: str,
    chat_id: object,
    cq_id: str | None,
) -> list:
    """Phase 6.0E-C · 共享派发 helper：把 callback_data 跑过 work_state，
    然后按调用来源（Inline 还是 Reply Keyboard）组装回执。

    cq_id != None  → Inline Keyboard 路径：AnswerCallback toast +（success 时）
                     额外 SendMessage 双语状态消息（6.0E-B 行为不变）
    cq_id == None  → Reply Keyboard 路径：没有 callback_query 可应答 → 直接
                     SendMessage：success 时发完整双语状态消息；失败时发短的
                     双语失败提示
    """
    result = _dispatch_action(db, employee_id, callback_data)
    success = result is not None and getattr(result, "changed", False)
    actions: list = []

    # Phase 6.5A · UX-003：成功的手动 Punch 登记到 manual_punch，60s 内
    # 抑制 idle_exit 通知（避免 BTW 后 "Working" + "Active 恢复工作" 双消息）
    if success:
        manual_punch.mark(employee_id)
        # UX-004：记录(员工, action)成功时刻，用于吞掉随后双击产生的重复 noop
        punch_debounce.mark(employee_id, callback_data)

    # Inline path: 总是回 toast
    if cq_id is not None:
        if success:
            toast = _build_action_toast(callback_data) or _TOAST_FAIL
        else:
            # GS-12 · noop → 友好提示；未识别动作 → _TOAST_FAIL（_noop_feedback 内部判）
            toast = _noop_feedback(result, callback_data)
        actions.append(AnswerCallback(cq_id, toast))

    # 发往群聊的可见消息
    if chat_id is not None:
        if success:
            chat_msg = _build_action_chat_message(callback_data, employee_name)
            if chat_msg is not None:
                actions.append(SendMessage(chat_id, chat_msg))
        elif cq_id is None:
            # Reply Keyboard 路径：没有 toast 通道 → 用 SendMessage 反馈（GS-12 友好提示）。
            # UX-004：若是同一动作刚成功后窗口内的重复点击（双击）→ 静默吞掉这条重复 noop，
            # 避免「成功卡片 + Already On...」自相矛盾的双消息。真正的延后重试仍正常反馈。
            if not punch_debounce.is_recent_duplicate(employee_id, callback_data):
                actions.append(SendMessage(chat_id, _noop_feedback(result, callback_data)))

    return actions


def _handle_callback(db: Session, cq: dict) -> list:
    cq_id = cq.get("id")
    try:
        from_user = cq.get("from") or {}
        tg_id = from_user.get("id")
        username = from_user.get("username")
        data = cq.get("data") or ""

        _safe_seen(db, tg_id, username)
        employee_id = (
            telegram_bindings.get_employee_id_by_tg(db, tg_id)
            if tg_id is not None
            else None
        )
        if employee_id is None:
            return [AnswerCallback(cq_id, _TOAST_UNBOUND, show_alert=True)]
        employee = db.get(Employee, employee_id)
        if employee is None or not employee.is_active:
            return [AnswerCallback(cq_id, _TOAST_INACTIVE, show_alert=True)]

        chat_id = ((cq.get("message") or {}).get("chat") or {}).get("id")
        return _execute_punch_action(
            db, employee_id, employee.name,
            callback_data=data,
            chat_id=chat_id,
            cq_id=cq_id,
        )
    except Exception:  # noqa: BLE001  保证一定回 answer，避免客户端转圈
        logger.exception("处理 callback 失败")
        return [AnswerCallback(cq_id, _TOAST_FAIL)] if cq_id else []


def _handle_message(db: Session, msg: dict) -> list:
    """处理普通文字消息。

    Phase 6.0E-C 与 BotFather Group Privacy=off 协同：bot 会收到群里**所有**
    文字消息（非仅 /command）。为避免群里刷屏，本函数**只对 9 类文本回应**：
        - /help (含 /help@Bot)
        - /start, /menu (含 @Bot 后缀)
        - 6 个 Reply Keyboard 按钮 label（_REPLY_BUTTON_TO_ACTION 字典 key）
    其它一律返回 [] 沉默。
    """
    try:
        from_user = msg.get("from") or {}
        tg_id = from_user.get("id")
        username = from_user.get("username")
        chat = msg.get("chat") or {}
        chat_id = chat.get("id")
        text = (msg.get("text") or "").strip()

        # Phase 5.6A: /help 是公共命令（不查 DB，未绑定用户也能调用，便于探活）
        cmd_raw = text.split()[0].lower() if text else ""
        cmd_norm = cmd_raw.split("@", 1)[0]  # 兼容 /help@BotName 形式
        if cmd_norm == "/help" and chat_id is not None:
            return [SendMessage(chat_id, _HELP_TEXT)]

        # Phase 6.0E-C 关键防 spam：群里 privacy off 后 bot 看到所有文字，
        # 我们只在 (a) /start /menu (b) 6 按钮文本之一 时介入；其它沉默
        is_start_cmd = cmd_norm in ("/menu", "/start")
        is_punch_button = text in _REPLY_BUTTON_TO_ACTION
        if not (is_start_cmd or is_punch_button):
            return []

        _safe_seen(db, tg_id, username)
        employee_id = (
            telegram_bindings.get_employee_id_by_tg(db, tg_id)
            if tg_id is not None
            else None
        )
        if employee_id is None:
            # Phase 6.0E-C: 用 6.0E-B 的双语 toast 文本，避免 _UNBOUND_HINT 单中文
            return [SendMessage(chat_id, _TOAST_UNBOUND)] if chat_id is not None else []
        employee = db.get(Employee, employee_id)
        if employee is None or not employee.is_active:
            return [SendMessage(chat_id, _TOAST_INACTIVE)] if chat_id is not None else []

        # /start /menu → 双语欢迎语 + 持久 Reply Keyboard
        # 旧 build_menu_keyboard()（Inline）保留供未来需要，但默认走 Reply
        if is_start_cmd:
            return [
                SendMessage(
                    chat_id, _START_GREETING,
                    reply_markup=build_reply_keyboard(),
                )
            ]

        # Reply Keyboard 按钮被按下 → 文本反查 → 与 Inline 路径走同一 helper
        action_cd = _REPLY_BUTTON_TO_ACTION[text]
        return _execute_punch_action(
            db, employee_id, employee.name,
            callback_data=action_cd,
            chat_id=chat_id,
            cq_id=None,  # Reply 路径无 callback_query
        )
    except Exception:  # noqa: BLE001
        logger.exception("处理 message 失败")
        return []


def handle_update(db: Session, update: dict) -> list:
    """派发单条 update，返回发送意图列表（poller 负责实际发送）。"""
    if "callback_query" in update:
        return _handle_callback(db, update["callback_query"])
    if "message" in update:
        return _handle_message(db, update["message"])
    return []

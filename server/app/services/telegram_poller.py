"""Telegram 长轮询守护线程（getUpdates，非 webhook）。

- offset 持久化在 settings 表 key=telegram_update_offset（绕过 settings_service 白名单，
  不出现在 /settings）；重启续传，不重复消费。
- 首启可跳过历史积压（telegram_skip_backlog_on_start）。
- 每条 update 独立 try/catch：坏 update 不阻塞 offset 推进；callback 出错兜底 answer。
- 网络/token 错误：记日志、退避，线程持续存活不崩。
"""
import logging
import threading

from ..config import settings
from ..database import SessionLocal
from ..models import Setting
from . import telegram_client, telegram_handlers

logger = logging.getLogger(__name__)

_OFFSET_KEY = "telegram_update_offset"


class TelegramPoller:
    def __init__(self) -> None:
        self._thread: threading.Thread | None = None
        self._stop = threading.Event()

    def start(self) -> None:
        if not settings.telegram_enabled or not settings.telegram_bot_token:
            logger.info("Telegram 未启用或未配置 token，poller 跳过启动")
            return
        if self._thread and self._thread.is_alive():
            return
        self._stop.clear()
        self._thread = threading.Thread(
            target=self._run, name="telegram-poller", daemon=True
        )
        self._thread.start()
        logger.info("Telegram poller 已启动")

    def stop(self) -> None:
        self._stop.set()
        if self._thread:
            self._thread.join(timeout=settings.telegram_poll_timeout_seconds + 15)
        logger.info("Telegram poller 已停止")

    # ---- offset 持久化 ----

    def _load_offset(self, db) -> int | None:
        row = db.get(Setting, _OFFSET_KEY)
        if row is None:
            return None
        try:
            return int(row.value)
        except (TypeError, ValueError):
            return None

    def _save_offset(self, db, offset: int) -> None:
        row = db.get(Setting, _OFFSET_KEY)
        if row is None:
            row = Setting(
                key=_OFFSET_KEY,
                value=str(offset),
                value_type="int",
                description="Telegram getUpdates offset（poller 内部用）",
            )
            db.add(row)
        else:
            row.value = str(offset)
        db.commit()

    def _persist_offset(self, offset: int) -> None:
        db = SessionLocal()
        try:
            self._save_offset(db, offset)
        except Exception:  # noqa: BLE001
            logger.exception("持久化 offset 失败")
        finally:
            db.close()

    def _init_offset(self) -> int | None:
        """已有 offset 则续传；否则按需跳过积压。"""
        db = SessionLocal()
        try:
            offset = self._load_offset(db)
            if offset is not None:
                return offset
            if settings.telegram_skip_backlog_on_start:
                try:
                    updates = telegram_client.get_updates(offset=-1, timeout=0)
                    if updates:
                        offset = updates[-1]["update_id"] + 1
                        self._save_offset(db, offset)
                        logger.info("首启跳过积压，offset=%s", offset)
                        return offset
                except Exception:  # noqa: BLE001
                    logger.exception("首启获取最新 offset 失败，将从头开始")
            return None
        finally:
            db.close()

    # ---- 主循环 ----

    def _run(self) -> None:
        if settings.telegram_send_menu_on_start and settings.telegram_chat_id:
            self._try_send_menu()

        offset = self._init_offset()
        while not self._stop.is_set():
            try:
                updates = telegram_client.get_updates(
                    offset=offset, timeout=settings.telegram_poll_timeout_seconds
                )
            except Exception:  # noqa: BLE001  网络/token 错误：退避后继续，线程不崩
                logger.exception(
                    "getUpdates 失败，%s 秒后重试", settings.telegram_poll_backoff_seconds
                )
                self._stop.wait(settings.telegram_poll_backoff_seconds)
                continue

            if not updates:
                continue

            for update in updates:
                self._process_one(update)
                # 无论该 update 处理成败，offset 都推进，坏消息不阻塞
                offset = update["update_id"] + 1
            self._persist_offset(offset)

    def _process_one(self, update: dict) -> None:
        db = SessionLocal()
        try:
            for action in telegram_handlers.handle_update(db, update):
                self._execute(action)
        except Exception:  # noqa: BLE001
            logger.exception("处理 update 失败 update_id=%s", update.get("update_id"))
            cq = update.get("callback_query")
            if cq and cq.get("id"):
                try:
                    telegram_client.answer_callback_query(cq["id"], text="操作失败，请稍后重试")
                except Exception:  # noqa: BLE001
                    logger.exception("兜底 answerCallbackQuery 失败")
        finally:
            db.close()

    def _execute(self, action) -> None:
        if isinstance(action, telegram_handlers.SendMessage):
            telegram_client.send_message(action.chat_id, action.text, action.reply_markup)
        elif isinstance(action, telegram_handlers.AnswerCallback):
            telegram_client.answer_callback_query(
                action.callback_query_id, action.text, action.show_alert
            )

    def _try_send_menu(self) -> None:
        try:
            telegram_client.send_message(
                settings.telegram_chat_id, "打卡面板：", telegram_handlers.build_menu_keyboard()
            )
        except Exception:  # noqa: BLE001
            logger.exception("启动发送面板失败")


telegram_poller = TelegramPoller()

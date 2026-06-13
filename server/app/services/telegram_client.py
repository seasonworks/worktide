"""Telegram Bot API 薄封装（urllib，无第三方依赖）。

只负责 HTTP，不含任何业务逻辑；独立于 notifier.py（不为复用而改动现有稳定逻辑）。
所有调用都带 timeout。失败抛 RuntimeError/URLError，由调用方（poller）捕获重试。
"""
import json
import logging
import urllib.request

from ..config import settings

logger = logging.getLogger(__name__)

_API = "https://api.telegram.org/bot{token}/{method}"


def _call(method: str, payload: dict, timeout: int | None = None):
    token = settings.telegram_bot_token
    if not token:
        raise RuntimeError("Telegram bot token 未配置")
    url = _API.format(token=token, method=method)
    data = json.dumps(payload).encode("utf-8")
    req = urllib.request.Request(
        url, data=data, headers={"Content-Type": "application/json"}
    )
    timeout = timeout if timeout is not None else settings.telegram_timeout_seconds
    with urllib.request.urlopen(req, timeout=timeout) as resp:  # noqa: S310 固定 https 域名
        body = json.loads(resp.read().decode("utf-8"))
    if not body.get("ok"):
        raise RuntimeError(f"Telegram API {method} 返回 not ok: {body}")
    return body.get("result")


def get_updates(offset: int | None = None, timeout: int = 30) -> list[dict]:
    """长轮询拉取更新。socket 超时设得比长轮询久，避免提前断开。"""
    payload = {"timeout": timeout, "allowed_updates": ["message", "callback_query"]}
    if offset is not None:
        payload["offset"] = offset
    return _call("getUpdates", payload, timeout=timeout + 10) or []


def send_message(chat_id, text: str, reply_markup: dict | None = None):
    payload = {"chat_id": chat_id, "text": text}
    if reply_markup is not None:
        payload["reply_markup"] = reply_markup
    return _call("sendMessage", payload)


def answer_callback_query(callback_query_id: str, text: str | None = None, show_alert: bool = False):
    payload = {"callback_query_id": callback_query_id, "show_alert": show_alert}
    if text:
        payload["text"] = text
    return _call("answerCallbackQuery", payload)

"""Phase 5.6A · Standalone Telegram bot entrypoint.

Runs the long-poller in its own process, completely decoupled from the
FastAPI app (uvicorn). Wired via systemd unit `worktide-telegram.service`.

Why a separate process:
  - Telegram getUpdates is mutually exclusive per bot token; one poller only.
  - The API process keeps `TELEGRAM_ENABLED=false` so it does NOT start a
    second poller. This file is the sole long-poller in the deployment.
  - Crash isolation: a Telegram outage or poller bug cannot take down the
    API; conversely, an API restart does not interrupt the bot.

Shared with the API:
  - Same SQLite DB (WAL mode + busy_timeout=5000) → safe for concurrent
    writers (work_state clock_in/out, telegram_users seen, settings offset).
  - Same code path: imports `app.services.telegram_poller.telegram_poller`
    and `app.services.telegram_handlers` unchanged.

Run:  uvicorn-equivalent → just `python -m bot_main` (or `python bot_main.py`).
"""

from __future__ import annotations

import logging
import os
import signal
import sys
import threading

# Ensure `from app...` resolves when launched from the server/ directory
_THIS_DIR = os.path.dirname(os.path.abspath(__file__))
if _THIS_DIR not in sys.path:
    sys.path.insert(0, _THIS_DIR)

from app import models  # noqa: F401  register ORM models before create_all
from app.config import settings
from app.database import Base, engine
from app.logging_config import setup_logging
from app.services.telegram_poller import telegram_poller

_log = logging.getLogger("worktide.bot")


def main() -> int:
    setup_logging()  # routes to LOG_DIR/server.log + journal (StandardOutput=journal)

    if not settings.telegram_enabled:
        _log.error(
            "TELEGRAM_ENABLED=false; refusing to start a bot-only process. "
            "Set TELEGRAM_ENABLED=true in your environment file."
        )
        return 2
    if not settings.telegram_bot_token:
        _log.error("TELEGRAM_BOT_TOKEN unset; refusing to start.")
        return 2

    # Ensure schema present (idempotent; harmless if API created it already).
    Base.metadata.create_all(bind=engine)

    stop_event = threading.Event()

    def _shutdown(signum, _frame):
        _log.info("signal %s received → stopping poller", signum)
        stop_event.set()

    signal.signal(signal.SIGTERM, _shutdown)
    signal.signal(signal.SIGINT, _shutdown)

    telegram_poller.start()
    _log.info(
        "worktide-telegram started: pid=%d enabled=%s skip_backlog=%s",
        os.getpid(),
        settings.telegram_enabled,
        settings.telegram_skip_backlog_on_start,
    )

    try:
        stop_event.wait()  # block forever until signal
    finally:
        telegram_poller.stop()
        _log.info("worktide-telegram exited cleanly")

    return 0


if __name__ == "__main__":
    sys.exit(main())

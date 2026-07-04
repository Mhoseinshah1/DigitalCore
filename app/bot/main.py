"""Telegram bot entrypoint (thin).

Builds the bot + dispatcher via app.bot.loader and runs long polling. Exits
cleanly (code 0) with a clear message when TELEGRAM_BOT_TOKEN is empty.
"""
from __future__ import annotations

import asyncio
import logging
import sys

from app.bot.loader import create_bot, create_dispatcher
from app.config import settings
from app.core.logging import configure_logging

configure_logging()
log = logging.getLogger("bot")


async def main() -> None:
    token = (settings.TELEGRAM_BOT_TOKEN or "").strip()
    if not token:
        log.warning(
            "TELEGRAM_BOT_TOKEN is not set. The bot has nothing to connect to and "
            "is exiting cleanly. Set TELEGRAM_BOT_TOKEN to enable it."
        )
        sys.exit(0)

    bot = create_bot(token)
    dp = create_dispatcher()
    log.info("DigitalCore bot starting (long polling)…")
    await dp.start_polling(bot)


if __name__ == "__main__":
    asyncio.run(main())

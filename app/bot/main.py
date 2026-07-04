"""Telegram bot (Phase 1): minimal, reliable connectivity only.

- Requires TELEGRAM_BOT_TOKEN. If it is empty, logs a clear message and exits
  gracefully (exit code 0) instead of crashing.
- /start -> "DigitalCore bot is running."
- /ping  -> "pong"

No product/downloader features yet — those arrive in a later phase.
"""
from __future__ import annotations

import asyncio
import logging
import sys

from aiogram import Bot, Dispatcher, F
from aiogram.filters import CommandStart, Command
from aiogram.types import Message

from app.config import settings

logging.basicConfig(level=logging.INFO, format="%(levelname)s %(name)s %(message)s")
log = logging.getLogger("bot")

dp = Dispatcher()


@dp.message(CommandStart())
async def on_start(message: Message) -> None:
    await message.answer("DigitalCore bot is running.")


@dp.message(Command("ping"))
async def on_ping(message: Message) -> None:
    await message.answer("pong")


async def main() -> None:
    token = (settings.TELEGRAM_BOT_TOKEN or "").strip()
    if not token:
        log.warning(
            "TELEGRAM_BOT_TOKEN is not set. The bot has nothing to connect to and "
            "is exiting cleanly. Set TELEGRAM_BOT_TOKEN to enable it."
        )
        sys.exit(0)

    bot = Bot(token=token)
    log.info("DigitalCore bot starting (long polling)…")
    await dp.start_polling(bot)


if __name__ == "__main__":
    asyncio.run(main())

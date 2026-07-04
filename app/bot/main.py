"""Telegram bot skeleton.

Boots from BOT_TOKEN and reads its texts/flags from the same business settings the
web panel edits. This is the first-boot skeleton: it proves the token works,
greets users with the configured start text, respects maintenance mode, and points
the owner at the panel. Sales/payment/V2Ray/license flows are added in later phases
and will read their configuration from these same settings.
"""
from __future__ import annotations

import asyncio
import logging

from aiogram import Bot, Dispatcher, F
from aiogram.filters import CommandStart
from aiogram.types import Message

from app.config import settings
from app.core.settings_service import SettingsService
from app.database import SessionLocal

logging.basicConfig(level=logging.INFO, format="%(levelname)s %(name)s %(message)s")
log = logging.getLogger("bot")

dp = Dispatcher()


async def _get(key: str, default=None):
    async with SessionLocal() as session:
        return await SettingsService(session).get(key, default)


def _is_admin(telegram_id: int) -> bool:
    return telegram_id in settings.admin_telegram_ids


@dp.message(CommandStart())
async def on_start(message: Message) -> None:
    if await _get("maintenance_mode", False):
        await message.answer("🛠 The bot is currently under maintenance. Please try again later.")
        return

    start_text = await _get("start_text", "")
    if not start_text:
        start_text = "👋 Welcome to DigitalCore.\n\nThis platform is freshly installed."

    await message.answer(start_text)

    if _is_admin(message.from_user.id):
        await message.answer(
            "You are an admin. 🌐 Configure cards, channels, plans, V2Ray/3X-UI "
            f"servers, licenses and texts in the web panel:\n{settings.WEB_PANEL_URL}"
        )


@dp.message(F.text == "/rules")
async def on_rules(message: Message) -> None:
    text = await _get("rules_text", "")
    await message.answer(text or "No rules have been configured yet.")


@dp.message(F.text == "/support")
async def on_support(message: Message) -> None:
    text = await _get("support_text", "")
    username = await _get("support_admin_username", "")
    if not text and username:
        text = f"Contact support: {username}"
    await message.answer(text or "Support is not configured yet.")


async def main() -> None:
    if not settings.BOT_TOKEN:
        log.error("BOT_TOKEN is not set; the bot cannot start.")
        return
    for warning in settings.insecure_config_warnings():
        log.warning("INSECURE CONFIG: %s", warning)
    bot = Bot(token=settings.BOT_TOKEN)

    for admin_id in settings.admin_telegram_ids:
        try:
            await bot.send_message(
                admin_id,
                "✅ DigitalCore is up. Configure the business in the panel: "
                f"{settings.WEB_PANEL_URL}",
            )
        except Exception as exc:  # noqa: BLE001 - admin may not have opened the bot yet
            log.info("Could not notify admin %s: %s", admin_id, exc)

    log.info("Bot starting (long polling)…")
    await dp.start_polling(bot)


if __name__ == "__main__":
    asyncio.run(main())

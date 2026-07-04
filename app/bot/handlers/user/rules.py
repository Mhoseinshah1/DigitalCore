"""/rules and the ℹ️ Rules button — text comes from the rules_text setting."""
from __future__ import annotations

from aiogram import F, Router
from aiogram.filters import Command
from aiogram.types import Message

from app.bot.keyboards.user import BTN_RULES
from app.core.settings_service import SettingsService
from app.database import SessionLocal

router = Router(name="user.rules")

DEFAULT_RULES_TEXT = "ℹ️ No rules have been configured yet."


@router.message(Command("rules"))
@router.message(F.text == BTN_RULES)
async def on_rules(message: Message) -> None:
    async with SessionLocal() as session:
        text = await SettingsService(session).get_str("rules_text", "")
    await message.answer(text or DEFAULT_RULES_TEXT)

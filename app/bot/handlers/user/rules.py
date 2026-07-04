"""/rules and the ℹ️ Rules button — text comes from the rules_text setting."""
from __future__ import annotations

from collections.abc import Callable

from aiogram import F, Router
from aiogram.filters import Command
from aiogram.types import Message

from app.core.settings_service import SettingsService
from app.database import SessionLocal
from app.i18n import texts_for

router = Router(name="user.rules")


@router.message(Command("rules"))
@router.message(F.text.in_(texts_for("btn.rules")))
async def on_rules(message: Message, _: Callable[..., str]) -> None:
    async with SessionLocal() as session:
        text = await SettingsService(session).get_str("rules_text", "")
    await message.answer(text or _("rules.empty"))

"""/start and /ping. Thin: registration goes through app/services."""
from __future__ import annotations

import logging

from aiogram import Router
from aiogram.filters import Command, CommandStart
from aiogram.types import Message

from app.core.permissions import Role
from app.core.settings_service import SettingsService
from app.database import SessionLocal
from app.bot.keyboards.user import user_main_menu
from app.services import audit_service, user_service

log = logging.getLogger("bot.start")

router = Router(name="user.start")

DEFAULT_START_TEXT = "👋 Welcome to DigitalCore!"


@router.message(CommandStart())
async def on_start(message: Message, role: Role | None = None) -> None:
    tg_user = message.from_user
    if tg_user is None:
        return

    async with SessionLocal() as session:
        user, created = await user_service.register_or_update_user(
            session,
            telegram_id=tg_user.id,
            username=tg_user.username,
            first_name=tg_user.first_name,
            last_name=tg_user.last_name,
        )
        if created:
            await audit_service.log(
                session,
                actor_type="user",
                actor_id=tg_user.id,
                action="user.registered",
                target_type="user",
                target_id=user.id,
            )
        start_text = await SettingsService(session).get("start_text", "") or DEFAULT_START_TEXT

    await message.answer(start_text, reply_markup=user_main_menu(is_admin=role is not None))


@router.message(Command("ping"))
async def on_ping(message: Message) -> None:
    await message.answer("✅ pong — DigitalCore bot is running.")

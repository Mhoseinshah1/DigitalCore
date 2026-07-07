"""/start and /ping. Thin: registration goes through app/services."""
from __future__ import annotations

import logging
from collections.abc import Callable

from aiogram import Router
from aiogram.filters import Command, CommandObject, CommandStart
from aiogram.types import Message

from app.bot.handlers.user.language import language_picker_keyboard
from app.bot.keyboards.user import user_main_menu
from app.core.settings_service import SettingsService
from app.database import SessionLocal
from app.services import audit_service, referral_service, user_service

log = logging.getLogger("bot.start")

router = Router(name="user.start")


@router.message(CommandStart())
async def on_start(
    message: Message, _: Callable[..., str], command: CommandObject | None = None,
    lang: str = "fa", is_admin: bool = False
) -> None:
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
            language_code=tg_user.language_code,
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
        # Referral deep link (Phase 10): /start ref_<code>. Safe/idempotent —
        # ignores an invalid code / self-referral and never overwrites a referrer.
        ref_code = referral_service.parse_start_code(command.args if command else None)
        if ref_code:
            try:
                if await referral_service.register_referral(session, user.id, ref_code):
                    await session.commit()
            except Exception as exc:  # noqa: BLE001 - referral must never block /start
                log.info("referral registration skipped: %s", exc)
        start_text = await SettingsService(session).get_str("start_text", "")

    if created:
        # New user: pick a language first; the menu follows in that language.
        await message.answer(_("lang.pick"), reply_markup=language_picker_keyboard())
        return

    await message.answer(
        start_text or _("greeting"),
        reply_markup=user_main_menu(lang, is_admin=is_admin),
    )


@router.message(Command("ping"))
async def on_ping(message: Message, _: Callable[..., str]) -> None:
    await message.answer(_("ping"))

"""Phase 10 user referrals: /referral — show the invite link, code, and stats.

The referral registration itself happens in start.py on ``/start ref_<code>``.
"""
from __future__ import annotations

import logging
from collections.abc import Callable

from aiogram import Bot, F, Router
from aiogram.filters import Command
from aiogram.fsm.context import FSMContext
from aiogram.types import Message

from app.core.settings_service import SettingsService
from app.database import SessionLocal
from app.i18n import texts_for
from app.services import referral_service, user_service

log = logging.getLogger("bot.user.referral")

router = Router(name="user.referral")


async def _bot_username(bot: Bot | None) -> str:
    if bot is None:
        return ""
    try:
        me = await bot.get_me()
        return me.username or ""
    except Exception as exc:  # noqa: BLE001
        log.info("get_me failed: %s", exc)
        return ""


@router.message(Command("referral"))
@router.message(F.text.in_(texts_for("btn.referral")))
async def on_referral(
    message: Message, bot: Bot, _: Callable[..., str], state: FSMContext, lang: str = "fa"
) -> None:
    await state.clear()
    tg = message.from_user
    async with SessionLocal() as session:
        if not await referral_service.referrals_enabled(session):
            await message.answer(_("referral.disabled"))
            return
        user, _c = await user_service.create_or_update_from_telegram(
            session, telegram_id=tg.id, username=tg.username,
            first_name=tg.first_name, last_name=tg.last_name)
        await session.commit()
        code = await referral_service.get_or_create_referral_code(session, user.id)
        stats = await referral_service.referral_stats(session, user.id)
        # Prefer the bot username from a setting, else ask Telegram.
        username = (await SettingsService(session).get_str("bot_username", "")).strip()

    username = username or await _bot_username(bot)
    link = (f"https://t.me/{username}?start=ref_{code}" if username and code
            else _("referral.no_link"))

    lines = [
        _("referral.title"), "",
        _("referral.code", code=code or "—"),
        _("referral.link", link=link), "",
        _("referral.stat.invited", n=stats["invited"]),
        _("referral.stat.paid", n=stats["paid_referrals"]),
        _("referral.stat.total_rewards", amount=f"{stats['total_rewards']:,}"),
        _("referral.stat.pending_rewards", amount=f"{stats['pending_rewards']:,}"),
    ]
    await message.answer("\n".join(lines), parse_mode="HTML")

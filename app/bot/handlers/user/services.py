"""Phase 6 user bot: /my_services — list purchased V2Ray services and re-fetch
the subscription link + QR for one. Never exposes another user's service.
"""
from __future__ import annotations

import logging
from collections.abc import Callable

from aiogram import F, Router
from aiogram.filters import Command
from aiogram.fsm.context import FSMContext
from aiogram.types import (
    CallbackQuery,
    InlineKeyboardButton,
    InlineKeyboardMarkup,
    Message,
)

from app.database import SessionLocal
from app.i18n import texts_for
from app.services import user_service, v2ray_service

CB_SERVICE = "usvc:"

log = logging.getLogger("bot.user.services")

router = Router(name="user.services")


@router.message(Command("my_services"))
@router.message(F.text.in_(texts_for("btn.my_services")))
async def on_my_services(
    message: Message, _: Callable[..., str], state: FSMContext, lang: str = "fa"
) -> None:
    await state.clear()
    tg_user = message.from_user
    async with SessionLocal() as session:
        user = await user_service.get_by_telegram_id(session, tg_user.id)
        services = await v2ray_service.list_user_services(session, user.id) if user else []
    services = [s for s in services if s.status != "deleted"]

    if not services:
        await message.answer(_("services.user.empty"))
        return

    lines = [_("services.user.title"), ""]
    buttons: list[list[InlineKeyboardButton]] = []
    for svc in services:
        title = svc.product.title if svc.product else "—"
        expire = svc.expire_at.strftime("%Y-%m-%d") if svc.expire_at else "—"
        lines.append(_("services.user.row", title=title,
                       status=_("service.status." + svc.status), expire=expire))
        buttons.append([InlineKeyboardButton(text=title, callback_data=f"{CB_SERVICE}{svc.id}")])
    await message.answer(
        "\n".join(lines), parse_mode="HTML",
        reply_markup=InlineKeyboardMarkup(inline_keyboard=buttons),
    )


@router.callback_query(F.data.startswith(CB_SERVICE))
async def on_service_detail(
    callback: CallbackQuery, _: Callable[..., str], lang: str = "fa"
) -> None:
    service_id = int((callback.data or "0")[len(CB_SERVICE):])
    tg_user = callback.from_user
    text: str | None = None
    qr_path: str | None = None
    async with SessionLocal() as session:
        user = await user_service.get_by_telegram_id(session, tg_user.id)
        svc = await v2ray_service.get_service(session, service_id)
        # Never reveal a service that is not this user's.
        if user is None or svc is None or svc.user_id != user.id:
            await callback.answer(_("services.user.not_found"), show_alert=True)
            return
        text = v2ray_service.build_service_message(svc.order, svc.product, svc, lang)
        qr_path = svc.qr_code_path
    if callback.message is not None and text:
        await callback.message.answer(text, parse_mode="HTML")
        if qr_path:
            try:
                from aiogram.types import FSInputFile
                await callback.message.answer_photo(FSInputFile(qr_path))
            except Exception as exc:  # noqa: BLE001 - QR is a bonus
                log.info("QR resend skipped: %s", exc)
        await callback.message.answer(_("services.user.renew_soon"))
    await callback.answer()

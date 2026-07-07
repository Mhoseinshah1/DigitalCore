"""User bot: /my_services — list purchased V2Ray services, show live usage +
expiry, resend the subscription link/QR, and start a renew / add-traffic purchase
(Phase 8). Never exposes another user's service.
"""
from __future__ import annotations

import logging
from collections.abc import Callable

from aiogram import Bot, F, Router
from aiogram.filters import Command
from aiogram.fsm.context import FSMContext
from aiogram.types import (
    CallbackQuery,
    InlineKeyboardButton,
    InlineKeyboardMarkup,
    Message,
)

from app.bot.handlers.user.orders import _start_action_payment, action_purchase_cb
from app.database import SessionLocal
from app.i18n import texts_for
from app.services import (
    product_service,
    user_service,
    v2ray_lifecycle_service,
    v2ray_service,
)

CB_SERVICE = "usvc:"        # service detail
CB_REFRESH = "usvcr:"       # refresh usage from the panel
CB_LINK = "usvcl:"          # resend subscription link + QR
CB_RENEW = "usvcn:"         # list renewal plans for a service
CB_ADD = "usvca:"           # list add-traffic plans for a service

GB = 1024 ** 3

log = logging.getLogger("bot.user.services")

router = Router(name="user.services")


def _gb(nbytes: int | None) -> str:
    return f"{int(nbytes or 0) / GB:.1f}"


def _service_or_none(svc, user):
    return svc is not None and user is not None and svc.user_id == user.id


def _detail_lines(svc, _: Callable[..., str]) -> list[str]:
    """Live usage + expiry summary for one service."""
    title = svc.product.title if svc.product else "—"
    expire = svc.expire_at.strftime("%Y-%m-%d") if svc.expire_at else "—"
    total = int(svc.total_gb or 0)
    lines = [
        _("services.user.detail.title", title=title),
        _("services.user.detail.status", status=_("service.status." + svc.status)),
        _("services.user.detail.expire", date=expire),
    ]
    days = v2ray_lifecycle_service.remaining_days(svc)
    if days is not None:
        lines.append(_("services.user.detail.remaining_days", days=days))
    if total <= 0:
        lines.append(_("services.user.detail.usage_unlimited", used=_gb(svc.used_gb)))
    else:
        rem = v2ray_lifecycle_service.remaining_bytes(svc)
        lines.append(_("services.user.detail.usage",
                       used=_gb(svc.used_gb), total=_gb(total)))
        lines.append(_("services.user.detail.remaining_traffic", gb=_gb(rem)))
    return lines


def _detail_keyboard(svc, _: Callable[..., str]) -> InlineKeyboardMarkup:
    rows: list[list[InlineKeyboardButton]] = [
        [InlineKeyboardButton(text=_("services.user.btn.get_link"),
                              callback_data=f"{CB_LINK}{svc.id}"),
         InlineKeyboardButton(text=_("services.user.btn.refresh"),
                              callback_data=f"{CB_REFRESH}{svc.id}")],
    ]
    # Renew / add-traffic only make sense while the service still exists.
    if svc.status != "deleted":
        rows.append([
            InlineKeyboardButton(text=_("services.user.btn.renew"),
                                 callback_data=f"{CB_RENEW}{svc.id}"),
            InlineKeyboardButton(text=_("services.user.btn.add_traffic"),
                                 callback_data=f"{CB_ADD}{svc.id}"),
        ])
    return InlineKeyboardMarkup(inline_keyboard=rows)


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
    lines: list[str] | None = None
    keyboard: InlineKeyboardMarkup | None = None
    async with SessionLocal() as session:
        user = await user_service.get_by_telegram_id(session, tg_user.id)
        svc = await v2ray_service.get_service(session, service_id)
        if not _service_or_none(svc, user):
            await callback.answer(_("services.user.not_found"), show_alert=True)
            return
        lines = _detail_lines(svc, _)
        keyboard = _detail_keyboard(svc, _)
    if callback.message is not None and lines:
        await callback.message.answer("\n".join(lines), parse_mode="HTML",
                                      reply_markup=keyboard)
    await callback.answer()


@router.callback_query(F.data.startswith(CB_LINK))
async def on_service_link(
    callback: CallbackQuery, _: Callable[..., str], lang: str = "fa"
) -> None:
    service_id = int((callback.data or "0")[len(CB_LINK):])
    tg_user = callback.from_user
    text: str | None = None
    qr_path: str | None = None
    async with SessionLocal() as session:
        user = await user_service.get_by_telegram_id(session, tg_user.id)
        svc = await v2ray_service.get_service(session, service_id)
        if not _service_or_none(svc, user):
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
    await callback.answer()


@router.callback_query(F.data.startswith(CB_REFRESH))
async def on_service_refresh(
    callback: CallbackQuery, _: Callable[..., str], lang: str = "fa"
) -> None:
    service_id = int((callback.data or "0")[len(CB_REFRESH):])
    tg_user = callback.from_user
    lines: list[str] | None = None
    keyboard: InlineKeyboardMarkup | None = None
    async with SessionLocal() as session:
        user = await user_service.get_by_telegram_id(session, tg_user.id)
        svc = await v2ray_service.get_service(session, service_id)
        if not _service_or_none(svc, user):
            await callback.answer(_("services.user.not_found"), show_alert=True)
            return
        # One user-triggered panel sync (never spammy — one call per tap).
        await v2ray_lifecycle_service.refresh_usage(session, service_id)
        svc = await v2ray_service.get_service(session, service_id)
        lines = _detail_lines(svc, _)
        keyboard = _detail_keyboard(svc, _)
    await callback.answer(_("services.user.refreshed"))
    if callback.message is not None and lines:
        await callback.message.answer("\n".join(lines), parse_mode="HTML",
                                      reply_markup=keyboard)


async def _show_action_plans(
    callback: CallbackQuery, _: Callable[..., str], service_id: int, action_type: str,
    prefix_key: str, none_key: str,
) -> None:
    """List the available renew / add-traffic plans as buy buttons."""
    tg_user = callback.from_user
    rows: list[list[InlineKeyboardButton]] = []
    async with SessionLocal() as session:
        user = await user_service.get_by_telegram_id(session, tg_user.id)
        svc = await v2ray_service.get_service(session, service_id)
        if not _service_or_none(svc, user):
            await callback.answer(_("services.user.not_found"), show_alert=True)
            return
        products = await product_service.list_service_action_products(session, action_type)
        for p in products:
            label = _("service.action.plan_row", title=p.title, price=f"{int(p.price or 0):,}")
            rows.append([InlineKeyboardButton(
                text=label, callback_data=action_purchase_cb(action_type, service_id, p.id))])
    await callback.answer()
    if callback.message is None:
        return
    if not rows:
        await callback.message.answer(_(none_key))
        return
    await callback.message.answer(
        _(prefix_key), reply_markup=InlineKeyboardMarkup(inline_keyboard=rows))


@router.callback_query(F.data.startswith(CB_RENEW))
async def on_service_renew(
    callback: CallbackQuery, _: Callable[..., str], lang: str = "fa"
) -> None:
    service_id = int((callback.data or "0")[len(CB_RENEW):])
    await _show_action_plans(callback, _, service_id, "renew_service",
                             "services.user.renew.pick", "services.user.renew.none")


@router.callback_query(F.data.startswith(CB_ADD))
async def on_service_add_traffic(
    callback: CallbackQuery, _: Callable[..., str], lang: str = "fa"
) -> None:
    service_id = int((callback.data or "0")[len(CB_ADD):])
    await _show_action_plans(callback, _, service_id, "add_traffic",
                             "services.user.addtraffic.pick", "services.user.addtraffic.none")

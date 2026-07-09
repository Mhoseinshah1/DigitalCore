"""«حساب من» (My Account): a summary of the user's account + quick links.

Fixes the previously dead main-menu button. Shows name, numeric Telegram id,
username, account status, wallet balance, and counts of orders / services /
licenses (the last labelled with the configurable license section title). The
admin note is never exposed. Inline buttons delegate to the existing section
renderers so nothing is duplicated.
"""
from __future__ import annotations

import html
from collections.abc import Callable

from aiogram import F, Router
from aiogram.filters import Command
from aiogram.fsm.context import FSMContext
from aiogram.types import CallbackQuery, InlineKeyboardButton, InlineKeyboardMarkup, Message

from app.bot.keyboards.user import license_section_title
from app.bot.utils.message_format import divider
from app.database import SessionLocal
from app.i18n import menu_texts
from app.services import (
    license_service,
    order_service,
    user_service,
    v2ray_service,
)

router = Router(name="user.account")

CB_WALLET = "acc:wallet"
CB_ORDERS = "acc:orders"
CB_SERVICES = "acc:services"
CB_LICENSES = "acc:licenses"
CB_SUPPORT = "acc:support"
CB_BACK = "acc:back"


def _status_key(user) -> str:
    if user.is_blocked:
        return "account.status.blocked"
    if user.is_restricted:
        return "account.status.restricted"
    return "account.status.active"


async def _account_summary(tg_user, _: Callable[..., str], lang: str) -> tuple[str, InlineKeyboardMarkup]:
    async with SessionLocal() as session:
        user, _created = await user_service.create_or_update_from_telegram(
            session, telegram_id=tg_user.id, username=tg_user.username,
            first_name=tg_user.first_name, last_name=tg_user.last_name,
        )
        await session.commit()
        orders = await order_service.list_user_orders(session, user.id)
        services = [s for s in await v2ray_service.list_user_services(session, user.id)
                    if s.status != "deleted"]
        licenses = await license_service.list_user_licenses(session, user.id)
        balance = int(user.wallet_balance or 0)
        name = " ".join(p for p in (user.first_name, user.last_name) if p) or "—"
        username = f"@{user.username}" if user.username else "—"
        telegram_id = user.telegram_id
        status = _(_status_key(user))
        restricted = user.is_restricted

    lic_title = await license_section_title(lang)
    lines = [
        _("account.title"),
        divider(),
        "",
        _("account.name", name=html.escape(name)),
        _("account.telegram_id", id=telegram_id),
        _("account.username", username=html.escape(username)),
        _("account.status", status=status),
        _("account.wallet", amount=f"{balance:,}"),
        _("account.orders_count", count=len(orders)),
        _("account.services_count", count=len(services)),
        _("account.licenses_count", title=lic_title, count=len(licenses)),
    ]
    if restricted:
        lines += ["", _("account.restricted_note")]

    kb = InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text=_("btn.wallet"), callback_data=CB_WALLET),
         InlineKeyboardButton(text=_("btn.my_orders"), callback_data=CB_ORDERS)],
        [InlineKeyboardButton(text=_("btn.my_services"), callback_data=CB_SERVICES),
         InlineKeyboardButton(text=lic_title, callback_data=CB_LICENSES)],
        [InlineKeyboardButton(text=_("btn.support"), callback_data=CB_SUPPORT)],
        [InlineKeyboardButton(text=_("btn.back"), callback_data=CB_BACK)],
    ])
    return "\n".join(lines), kb


@router.message(Command("account"))
@router.message(F.text.in_(menu_texts("btn.account")))
async def on_account(
    message: Message, _: Callable[..., str], state: FSMContext, lang: str = "fa"
) -> None:
    await state.clear()
    text, kb = await _account_summary(message.from_user, _, lang)
    await message.answer(text, parse_mode="HTML", reply_markup=kb)


@router.callback_query(F.data == CB_WALLET)
async def on_acc_wallet(callback: CallbackQuery, _: Callable[..., str], lang: str = "fa") -> None:
    from app.bot.handlers.user.wallet import render_wallet
    await callback.answer()
    if callback.message is not None:
        await render_wallet(callback.message, callback.from_user, _)


@router.callback_query(F.data == CB_ORDERS)
async def on_acc_orders(callback: CallbackQuery, _: Callable[..., str], lang: str = "fa") -> None:
    from app.bot.handlers.user.orders import render_orders
    await callback.answer()
    if callback.message is not None:
        await render_orders(callback.message, callback.from_user, _, lang)


@router.callback_query(F.data == CB_SERVICES)
async def on_acc_services(callback: CallbackQuery, _: Callable[..., str], lang: str = "fa") -> None:
    from app.bot.handlers.user.services import render_services
    await callback.answer()
    if callback.message is not None:
        await render_services(callback.message, callback.from_user, _, lang)


@router.callback_query(F.data == CB_LICENSES)
async def on_acc_licenses(callback: CallbackQuery, _: Callable[..., str], lang: str = "fa") -> None:
    from app.bot.handlers.user.orders import render_my_licenses
    await callback.answer()
    if callback.message is not None:
        await render_my_licenses(callback.message, callback.from_user, _, lang)


@router.callback_query(F.data == CB_SUPPORT)
async def on_acc_support(callback: CallbackQuery, _: Callable[..., str], lang: str = "fa") -> None:
    from app.bot.handlers.user.tickets import _show_support
    await callback.answer()
    if callback.message is not None:
        await _show_support(callback.message, _)


@router.callback_query(F.data == CB_BACK)
async def on_acc_back(callback: CallbackQuery, _: Callable[..., str], lang: str = "fa") -> None:
    await callback.answer()
    if callback.message is not None:
        try:
            await callback.message.delete()
        except Exception:  # noqa: BLE001 - deleting an old message may fail; ignore
            pass

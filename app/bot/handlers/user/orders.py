"""Phase 3 bot purchase flow: Buy -> order -> card-to-card instructions ->
receipt upload -> waiting_admin, plus /orders (My Orders).

Only card-to-card is supported. No delivery, approval, or provisioning happens
here — the receipt just lands in the admin review queue.
"""
from __future__ import annotations

import logging
from collections.abc import Callable
from io import BytesIO

from aiogram import Bot, F, Router
from aiogram.filters import BaseFilter, Command
from aiogram.fsm.context import FSMContext
from aiogram.fsm.state import State, StatesGroup
from aiogram.types import (
    CallbackQuery,
    InlineKeyboardButton,
    InlineKeyboardMarkup,
    Message,
)

from app.core.settings_service import SettingsService
from app.core.statuses import order_status_label
from app.database import SessionLocal
from app.i18n import menu_texts, t
from app.services import (
    coupon_service,
    license_service,
    order_service,
    payment_service,
    product_service,
    user_service,
    wallet_service,
)
from app.services.coupon_service import CouponError
from app.services.order_service import OrderError
from app.services.payment_service import ReceiptError, ReceiptFile

CB_LICENSE = "ulic:"

log = logging.getLogger("bot.user.orders")

router = Router(name="user.orders")

CB_BUY = "ubuy:"


class PurchaseStates(StatesGroup):
    waiting_for_receipt = State()
    waiting_for_coupon = State()


CB_COUPON_ENTER = "ucpn_e:"   # user wants to type a coupon code
CB_COUPON_SKIP = "ucpn_s:"    # continue without a coupon


def _coupon_error_text(exc: CouponError, _: Callable[..., str]) -> str:
    key = f"coupon.err.{exc.code}"
    text = _(key)
    return text if text != key else _("coupon.err.generic")


# Menu buttons/commands that must never be swallowed by the receipt-wait guidance.
_NAV_TEXTS: set[str] = set()
for _key in ("btn.products", "btn.account", "btn.support", "btn.rules",
             "btn.language", "btn.admin_panel", "btn.my_orders", "btn.my_licenses",
             "btn.my_services", "btn.wallet", "btn.tutorials", "btn.my_tickets",
             "btn.referral"):
    _NAV_TEXTS |= menu_texts(_key)


def _order_error_text(exc: OrderError, _: Callable[..., str]) -> str:
    """Map an OrderError.code to a specific message, else a generic one."""
    key = f"purchase.{exc.code}"
    text = _(key)
    return text if text != key else _("purchase.error", error=str(exc))


def _receipt_error_text(exc: ReceiptError, _: Callable[..., str]) -> str:
    key = f"purchase.receipt.{exc.code}"
    text = _(key)
    return text if text != key else _("purchase.receipt_rejected", error=str(exc))


def _payment_instruction_lines(order, product, cfg: dict[str, str], _: Callable[..., str]) -> list[str]:
    lines = [
        _("purchase.instructions_title"),
        "",
        _("purchase.order_number", number=order.order_number),
        _("purchase.product", title=product.title),
    ]
    if order.discount_amount:
        lines.append(_("purchase.original_amount", amount=f"{order.amount:,}"))
        lines.append(_("purchase.discount", code=order.coupon_code or "",
                       amount=f"{order.discount_amount:,}"))
    lines += [
        _("purchase.amount", amount=f"{order.final_amount:,}"),
        "",
        _("purchase.pay_header"),
        _("purchase.card_number", card=cfg["card_number"]),
    ]
    if cfg.get("card_owner"):
        lines.append(_("purchase.card_owner", owner=cfg["card_owner"]))
    if cfg.get("sheba_number"):
        lines.append(_("purchase.sheba", sheba=cfg["sheba_number"]))
    if cfg.get("payment_instructions"):
        lines.extend(["", cfg["payment_instructions"]])
    lines.extend(["", _("purchase.ask_receipt")])
    return lines


CB_PAY_CARD = "upayc:"
CB_PAY_WALLET = "upayw:"

# Invoice (پیش‌فاکتور) payment buttons, one per method (bot UX). These carry the
# chosen method through the coupon prompt before actually charging.
CB_INV_WALLET = "uinvw:"
CB_INV_CARD = "uinvc:"
CB_INV_GATEWAY = "uinvg:"


async def _apply_coupon_best_effort(session, order_id: int, coupon_code: str | None,
                                    user_id: int) -> None:
    """Apply a stashed coupon to a fresh order; on any coupon error, proceed at
    full price (the prompt already validated it — this guards a late change)."""
    if not coupon_code:
        return
    try:
        await coupon_service.apply_coupon_to_order(session, order_id, coupon_code, user_id)
        await session.commit()
    except CouponError:
        await session.rollback()


async def _start_card_payment(reply, tg_user, product_id: int, _: Callable[..., str],
                              state: FSMContext, *, action_type: str | None = None,
                              target_service_id: int | None = None,
                              coupon_code: str | None = None) -> None:
    """Create the card-to-card order + payment and show the transfer instructions.

    Extracted so the payment-method picker can reuse it. A renew/add-traffic order
    (Phase 8) passes ``action_type`` + ``target_service_id``; a coupon (Phase 10)
    is applied to the order before the Payment is created so the instructions and
    Payment.amount both reflect the discounted final_amount.
    """
    async with SessionLocal() as session:
        svc = SettingsService(session)
        if not await svc.get_bool("card_to_card_enabled", True):
            if reply is not None:
                await reply.answer(_("purchase.card_disabled"))
            return
        cfg = {
            "card_number": (await svc.get_str("card_number", "")).strip(),
            "card_owner": (await svc.get_str("card_owner", "")).strip(),
            "sheba_number": (await svc.get_str("sheba_number", "")).strip(),
            "payment_instructions": (await svc.get_str("payment_instructions", "")).strip(),
        }
        if not cfg["card_number"]:
            if reply is not None:
                await reply.answer(_("purchase.not_configured"))
            return

        user, _created = await user_service.create_or_update_from_telegram(
            session, telegram_id=tg_user.id, username=tg_user.username,
            first_name=tg_user.first_name, last_name=tg_user.last_name,
        )
        await session.commit()
        try:
            order = await order_service.create_order(
                session, user.id, product_id, action_type=action_type,
                target_service_id=target_service_id)
            await session.commit()
        except OrderError as exc:
            if reply is not None:
                await reply.answer(_order_error_text(exc, _))
            return
        # Apply the coupon (discounts final_amount) BEFORE the Payment is created.
        await _apply_coupon_best_effort(session, order.id, coupon_code, user.id)
        order = await order_service.get_order(session, order.id)
        await payment_service.create_payment_for_order(session, order)
        await session.commit()
        product = await product_service.get(session, product_id)

        # Payment Core: mint the invoice + unique tracking code for this order.
        from app.services import payment_core_service
        from app.services.template_render_service import format_toman, render_text_template
        invoice = await payment_core_service.create_product_invoice(
            session, user, product, order=order)
        payment = await payment_service.get_payment_by_order(session, order.id)
        if payment is not None:
            payment.invoice_id = invoice.id
            payment.payment_type = invoice.invoice_type
            if not payment.tracking_code:
                payment.tracking_code = payment_core_service.generate_tracking_code("PAY")
        await session.commit()
        tracking_code = payment.tracking_code if payment is not None else ""

        # Admin-configurable card text (falls back to the legacy built-in lines).
        template = (await svc.get_str("manual_receipt_text", "")).strip()
        if template:
            text = render_text_template(template, {
                "price": format_toman(order.final_amount),
                "card_number": cfg["card_number"],
                "name_card": cfg.get("card_owner") or "",
                "tracking_code": tracking_code,
                "invoice_number": invoice.invoice_number,
                "order_number": order.order_number,
                "username": tg_user.username or (tg_user.first_name or ""),
            })
            lines = [text, "", _("purchase.tracking_code", code=tracking_code)]
        else:
            lines = _payment_instruction_lines(order, product, cfg, _)
            if tracking_code:
                lines += ["", _("purchase.tracking_code", code=tracking_code)]
        card_number = cfg["card_number"]
        final_amount = int(order.final_amount or 0)
        order_id = order.id

    await state.set_state(PurchaseStates.waiting_for_receipt)
    await state.update_data(order_id=order_id, card_number=card_number,
                            pay_amount=final_amount)
    if reply is not None:
        keyboard = InlineKeyboardMarkup(inline_keyboard=[
            [InlineKeyboardButton(text=_("purchase.btn.copy_amount"),
                                  callback_data="paycpa"),
             InlineKeyboardButton(text=_("purchase.btn.copy_card"),
                                  callback_data="paycpc")],
            [InlineKeyboardButton(text=_("purchase.btn.paid"),
                                  callback_data="paydone")],
        ])
        await reply.answer("\n".join(lines), parse_mode="HTML", reply_markup=keyboard)


def _topup_button(_: Callable[..., str]) -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text=_("wallet.btn.topup"), callback_data="wtopup")],
    ])


async def _pay_with_wallet(reply, tg_user, product_id: int, _: Callable[..., str], bot,
                           *, action_type: str | None = None,
                           target_service_id: int | None = None,
                           coupon_code: str | None = None) -> None:
    """Charge the wallet for the product, then run the existing delivery flow.

    A renew/add-traffic order (Phase 8) passes ``action_type`` +
    ``target_service_id``; a coupon (Phase 10) discounts the order before the
    balance check + charge, so the wallet is charged the discounted final_amount."""
    async with SessionLocal() as session:
        user, _created = await user_service.create_or_update_from_telegram(
            session, telegram_id=tg_user.id, username=tg_user.username,
            first_name=tg_user.first_name, last_name=tg_user.last_name,
        )
        await session.commit()
        product = await product_service.get(session, product_id)
        if product is None:
            if reply is not None:
                await reply.answer(_("purchase.error", error="product"))
            return
        try:
            order = await order_service.create_order(
                session, user.id, product_id, payment_method="wallet",
                action_type=action_type, target_service_id=target_service_id)
            await session.commit()
        except OrderError as exc:
            if reply is not None:
                await reply.answer(_order_error_text(exc, _))
            return
        # Apply the coupon, then check the balance against the discounted amount.
        await _apply_coupon_best_effort(session, order.id, coupon_code, user.id)
        order = await order_service.get_order(session, order.id)
        amount = int(order.final_amount or 0)
        balance = await wallet_service.get_balance(session, user.id)
        if balance < amount:
            if reply is not None:
                await reply.answer(
                    _("purchase.wallet.insufficient", balance=f"{balance:,}", amount=f"{amount:,}"),
                    reply_markup=_topup_button(_))
            return
        try:
            result = await wallet_service.pay_order_with_wallet(
                session, order.id, user.id, bot=bot)
            await session.commit()
        except wallet_service.InsufficientBalanceError:
            await session.rollback()
            bal = await wallet_service.get_balance(session, user.id)
            if reply is not None:
                await reply.answer(
                    _("purchase.wallet.insufficient", balance=f"{bal:,}", amount=f"{amount:,}"),
                    reply_markup=_topup_button(_))
            return
        except wallet_service.WalletError as exc:
            await session.rollback()
            if reply is not None:
                await reply.answer(_("purchase.error", error=str(exc)))
            return
        order2 = await order_service.get_order(session, order.id)
        new_balance = result.get("balance", 0)
        delivered = bool(result.get("delivery", {}).get("delivered"))
        number, title = order2.order_number, product.title

    if reply is not None:
        key = "purchase.wallet.paid_delivered" if delivered else "purchase.wallet.paid_pending"
        await reply.answer(_(key, number=number, title=title, balance=f"{new_balance:,}"),
                           parse_mode="HTML")


async def _offer_payment_methods(reply, tg_user, product_id: int, _: Callable[..., str],
                                 state: FSMContext, bot, *, coupon_code: str | None) -> None:
    """Show the card/wallet picker, or go straight to the only enabled method.
    The chosen coupon is stashed in FSM so the picker callbacks can read it."""
    async with SessionLocal() as session:
        svc = SettingsService(session)
        card_ok = (await svc.get_bool("card_to_card_enabled", True)
                   and bool((await svc.get_str("card_number", "")).strip()))
        wallet_ok = (await svc.get_bool("wallet_enabled", True)
                     and await svc.get_bool("wallet_payment_enabled", True))
    await state.update_data(buy_product_id=product_id, buy_coupon=coupon_code)
    if wallet_ok and card_ok:
        if reply is not None:
            kb = InlineKeyboardMarkup(inline_keyboard=[
                [InlineKeyboardButton(text=_("purchase.method.card"),
                                      callback_data=f"{CB_PAY_CARD}{product_id}")],
                [InlineKeyboardButton(text=_("purchase.method.wallet"),
                                      callback_data=f"{CB_PAY_WALLET}{product_id}")],
            ])
            await reply.answer(_("purchase.choose_method"), reply_markup=kb)
        return
    if wallet_ok:
        await _pay_with_wallet(reply, tg_user, product_id, _, bot, coupon_code=coupon_code)
        return
    await _start_card_payment(reply, tg_user, product_id, _, state, coupon_code=coupon_code)


async def payment_method_rows(
    session, product_id: int, _: Callable[..., str]
) -> tuple[list[list[InlineKeyboardButton]], str | None]:
    """Invoice payment buttons for the currently enabled methods (bot UX).

    Returns ``(rows, note)``. ``note`` is a clear message when no payment method
    is available so the invoice never dead-ends with a lone Back button.
    """
    svc = SettingsService(session)
    card_ok = (await svc.get_bool("card_to_card_enabled", True)
               and bool((await svc.get_str("card_number", "")).strip()))
    wallet_ok = (await svc.get_bool("wallet_enabled", True)
                 and await svc.get_bool("wallet_payment_enabled", True))
    gateway_ok = await svc.get_bool("online_gateway_enabled", False)

    rows: list[list[InlineKeyboardButton]] = []
    if wallet_ok:
        rows.append([InlineKeyboardButton(
            text=_("btn.pay_wallet"), callback_data=f"{CB_INV_WALLET}{product_id}")])
    if card_ok:
        rows.append([InlineKeyboardButton(
            text=_("btn.pay_card"), callback_data=f"{CB_INV_CARD}{product_id}")])
    if gateway_ok:
        rows.append([InlineKeyboardButton(
            text=_("btn.pay_gateway"), callback_data=f"{CB_INV_GATEWAY}{product_id}")])
    note = None if rows else _("products.invoice.no_methods")
    return rows, note


async def _dispatch_method(reply, tg_user, product_id: int, method: str,
                           coupon_code: str | None, _: Callable[..., str],
                           state: FSMContext, bot) -> None:
    """Run the concrete payment for the chosen invoice method."""
    if method == "wallet":
        await _pay_with_wallet(reply, tg_user, product_id, _, bot, coupon_code=coupon_code)
    else:
        await _start_card_payment(reply, tg_user, product_id, _, state, coupon_code=coupon_code)


async def _begin_method_purchase(reply, tg_user, product_id: int, method: str,
                                 _: Callable[..., str], state: FSMContext, bot) -> None:
    """Invoice button tapped: prompt for a coupon (if enabled) then pay via `method`."""
    async with SessionLocal() as session:
        svc = SettingsService(session)
        if not await svc.get_bool("sales_enabled", True):
            if reply is not None:
                await reply.answer(_("purchase.sales_disabled"))
            return
        coupons_on = await svc.get_bool("coupons_enabled", True)
        product = await product_service.get(session, product_id)
    if product is None:
        if reply is not None:
            await reply.answer(_("products.unknown"))
        return
    await state.set_state(None)
    await state.update_data(buy_product_id=product_id, buy_coupon=None, buy_method=method)
    if coupons_on and reply is not None:
        kb = InlineKeyboardMarkup(inline_keyboard=[
            [InlineKeyboardButton(text=_("coupon.btn.enter"),
                                  callback_data=f"{CB_COUPON_ENTER}{product_id}")],
            [InlineKeyboardButton(text=_("coupon.btn.skip"),
                                  callback_data=f"{CB_COUPON_SKIP}{product_id}")],
        ])
        await reply.answer(
            _("coupon.prompt", price=f"{int(product.price or 0):,}"), reply_markup=kb)
        return
    await _dispatch_method(reply, tg_user, product_id, method, None, _, state, bot)


@router.callback_query(F.data.startswith(CB_INV_WALLET))
async def on_inv_wallet(
    callback: CallbackQuery, bot: Bot, _: Callable[..., str], state: FSMContext, lang: str = "fa"
) -> None:
    product_id = int((callback.data or "0")[len(CB_INV_WALLET):])
    await callback.answer()
    await _begin_method_purchase(callback.message, callback.from_user, product_id,
                                 "wallet", _, state, bot)


@router.callback_query(F.data.startswith(CB_INV_CARD))
async def on_inv_card(
    callback: CallbackQuery, bot: Bot, _: Callable[..., str], state: FSMContext, lang: str = "fa"
) -> None:
    product_id = int((callback.data or "0")[len(CB_INV_CARD):])
    await callback.answer()
    await _begin_method_purchase(callback.message, callback.from_user, product_id,
                                 "card", _, state, bot)


@router.callback_query(F.data.startswith(CB_INV_GATEWAY))
async def on_inv_gateway(
    callback: CallbackQuery, _: Callable[..., str], lang: str = "fa"
) -> None:
    """Online gateway is not implemented — show a clear placeholder either way."""
    async with SessionLocal() as session:
        enabled = await SettingsService(session).get_bool("online_gateway_enabled", False)
    await callback.answer(
        _("gateway.coming_soon") if enabled else _("gateway.disabled"), show_alert=True)


@router.callback_query(F.data.startswith(CB_BUY))
async def on_buy(
    callback: CallbackQuery, bot: Bot, _: Callable[..., str], state: FSMContext, lang: str = "fa"
) -> None:
    """Ask about a coupon first (if enabled), then offer the payment methods."""
    product_id = int((callback.data or "0")[len(CB_BUY):])
    reply = callback.message
    async with SessionLocal() as session:
        svc = SettingsService(session)
        if not await svc.get_bool("sales_enabled", True):
            await callback.answer(_("purchase.sales_disabled"), show_alert=True)
            return
        coupons_on = await svc.get_bool("coupons_enabled", True)
        product = await product_service.get(session, product_id)
    await callback.answer()
    if product is None:
        return
    await state.set_state(None)
    await state.update_data(buy_product_id=product_id, buy_coupon=None)
    if coupons_on and reply is not None:
        kb = InlineKeyboardMarkup(inline_keyboard=[
            [InlineKeyboardButton(text=_("coupon.btn.enter"),
                                  callback_data=f"{CB_COUPON_ENTER}{product_id}")],
            [InlineKeyboardButton(text=_("coupon.btn.skip"),
                                  callback_data=f"{CB_COUPON_SKIP}{product_id}")],
        ])
        await reply.answer(
            _("coupon.prompt", price=f"{int(product.price or 0):,}"), reply_markup=kb)
        return
    await _offer_payment_methods(reply, callback.from_user, product_id, _, state,
                                 bot, coupon_code=None)


@router.callback_query(F.data.startswith(CB_COUPON_SKIP))
async def on_coupon_skip(
    callback: CallbackQuery, bot: Bot, _: Callable[..., str], state: FSMContext, lang: str = "fa"
) -> None:
    product_id = int((callback.data or "0")[len(CB_COUPON_SKIP):])
    data = await state.get_data()
    method = data.get("buy_method")
    await state.set_state(None)
    await callback.answer()
    if method:
        await _dispatch_method(callback.message, callback.from_user, product_id, method,
                               None, _, state, bot)
        return
    await _offer_payment_methods(callback.message, callback.from_user, product_id, _, state,
                                 bot, coupon_code=None)


@router.callback_query(F.data.startswith(CB_COUPON_ENTER))
async def on_coupon_enter(
    callback: CallbackQuery, _: Callable[..., str], state: FSMContext, lang: str = "fa"
) -> None:
    product_id = int((callback.data or "0")[len(CB_COUPON_ENTER):])
    await state.update_data(buy_product_id=product_id)
    await state.set_state(PurchaseStates.waiting_for_coupon)
    await callback.answer()
    if callback.message is not None:
        await callback.message.answer(_("coupon.ask_code"))


@router.message(PurchaseStates.waiting_for_coupon, F.text, ~F.text.startswith("/"),
                ~F.text.in_(_NAV_TEXTS))
async def on_coupon_code(
    message: Message, bot: Bot, _: Callable[..., str], state: FSMContext, lang: str = "fa"
) -> None:
    data = await state.get_data()
    product_id = data.get("buy_product_id")
    if not product_id:
        await state.clear()
        return
    code = (message.text or "").strip()
    tg = message.from_user
    async with SessionLocal() as session:
        user = await user_service.get_by_telegram_id(session, tg.id)
        product = await product_service.get(session, product_id)
        if product is None:
            await state.clear()
            return
        try:
            coupon, discount = await coupon_service.validate_coupon(
                session, code, (user.id if user else 0), product_id,
                int(product.price or 0), action_type="new_purchase")
        except CouponError as exc:
            # Show the error and offer to retry or continue without a coupon.
            kb = InlineKeyboardMarkup(inline_keyboard=[[InlineKeyboardButton(
                text=_("coupon.btn.skip"), callback_data=f"{CB_COUPON_SKIP}{product_id}")]])
            await message.answer(_coupon_error_text(exc, _), reply_markup=kb)
            return
        price = int(product.price or 0)
        final = max(0, price - discount)
        norm_code = coupon.code
    method = data.get("buy_method")
    await state.set_state(None)
    await state.update_data(buy_coupon=norm_code)
    await message.answer(_("coupon.applied", code=norm_code, original=f"{price:,}",
                           discount=f"{discount:,}", final=f"{final:,}"))
    if method:
        await _dispatch_method(message, tg, product_id, method, norm_code, _, state, bot)
        return
    await _offer_payment_methods(message, tg, product_id, _, state, bot, coupon_code=norm_code)


async def _stashed_coupon(state: FSMContext, product_id: int) -> str | None:
    data = await state.get_data()
    if data.get("buy_product_id") == product_id:
        return data.get("buy_coupon")
    return None


@router.callback_query(F.data.startswith(CB_PAY_CARD))
async def on_pay_card(
    callback: CallbackQuery, _: Callable[..., str], state: FSMContext, lang: str = "fa"
) -> None:
    product_id = int((callback.data or "0")[len(CB_PAY_CARD):])
    coupon_code = await _stashed_coupon(state, product_id)
    await callback.answer()
    await _start_card_payment(callback.message, callback.from_user, product_id, _, state,
                              coupon_code=coupon_code)


@router.callback_query(F.data.startswith(CB_PAY_WALLET))
async def on_pay_wallet(
    callback: CallbackQuery, bot: Bot, _: Callable[..., str], state: FSMContext, lang: str = "fa"
) -> None:
    product_id = int((callback.data or "0")[len(CB_PAY_WALLET):])
    coupon_code = await _stashed_coupon(state, product_id)
    await callback.answer()
    await _pay_with_wallet(callback.message, callback.from_user, product_id, _, bot,
                           coupon_code=coupon_code)


# --------------------------------------------------------------------------
# Service-action purchase (Phase 8): renew / add-traffic on an existing service.
# Callback data is compact: ``uact:<r|t>:<service_id>:<product_id>``.
# --------------------------------------------------------------------------
CB_ACTION = "uact:"          # method picker for an action product
CB_ACTION_CARD = "uactc:"    # card-to-card for an action product
CB_ACTION_WALLET = "uactw:"  # wallet for an action product

_ACTION_BY_CHAR = {"r": "renew_service", "t": "add_traffic"}
_CHAR_BY_ACTION = {"renew_service": "r", "add_traffic": "t"}


def action_purchase_cb(action_type: str, service_id: int, product_id: int) -> str:
    """Build the method-picker callback for an action product."""
    return f"{CB_ACTION}{_CHAR_BY_ACTION[action_type]}:{service_id}:{product_id}"


def _parse_action_cb(data: str, prefix: str) -> tuple[str, int, int] | None:
    """(action_type, service_id, product_id) from ``<prefix><r|t>:<sid>:<pid>``."""
    try:
        char, sid, pid = data[len(prefix):].split(":", 2)
        return _ACTION_BY_CHAR[char], int(sid), int(pid)
    except (ValueError, KeyError):
        return None


async def _start_action_payment(
    reply, tg_user, action_type: str, service_id: int, product_id: int,
    _: Callable[..., str], state: FSMContext, bot,
) -> None:
    """Offer the method picker for an action order, or go straight to the only one."""
    async with SessionLocal() as session:
        svc = SettingsService(session)
        if not await svc.get_bool("sales_enabled", True):
            if reply is not None:
                await reply.answer(_("purchase.sales_disabled"))
            return
        card_ok = (await svc.get_bool("card_to_card_enabled", True)
                   and bool((await svc.get_str("card_number", "")).strip()))
        wallet_ok = (await svc.get_bool("wallet_enabled", True)
                     and await svc.get_bool("wallet_payment_enabled", True))
    char = _CHAR_BY_ACTION[action_type]
    if wallet_ok and card_ok:
        if reply is not None:
            kb = InlineKeyboardMarkup(inline_keyboard=[
                [InlineKeyboardButton(
                    text=_("purchase.method.card"),
                    callback_data=f"{CB_ACTION_CARD}{char}:{service_id}:{product_id}")],
                [InlineKeyboardButton(
                    text=_("purchase.method.wallet"),
                    callback_data=f"{CB_ACTION_WALLET}{char}:{service_id}:{product_id}")],
            ])
            await reply.answer(_("purchase.choose_method"), reply_markup=kb)
        return
    if wallet_ok:
        await _pay_with_wallet(reply, tg_user, product_id, _, bot,
                               action_type=action_type, target_service_id=service_id)
        return
    await _start_card_payment(reply, tg_user, product_id, _, state,
                              action_type=action_type, target_service_id=service_id)


@router.callback_query(F.data.startswith(CB_ACTION_CARD))
async def on_action_pay_card(
    callback: CallbackQuery, _: Callable[..., str], state: FSMContext, lang: str = "fa"
) -> None:
    parsed = _parse_action_cb(callback.data or "", CB_ACTION_CARD)
    await callback.answer()
    if parsed is None:
        return
    action_type, service_id, product_id = parsed
    await _start_card_payment(callback.message, callback.from_user, product_id, _, state,
                              action_type=action_type, target_service_id=service_id)


@router.callback_query(F.data.startswith(CB_ACTION_WALLET))
async def on_action_pay_wallet(
    callback: CallbackQuery, bot: Bot, _: Callable[..., str], lang: str = "fa"
) -> None:
    parsed = _parse_action_cb(callback.data or "", CB_ACTION_WALLET)
    await callback.answer()
    if parsed is None:
        return
    action_type, service_id, product_id = parsed
    await _pay_with_wallet(callback.message, callback.from_user, product_id, _, bot,
                           action_type=action_type, target_service_id=service_id)


@router.callback_query(F.data.startswith(CB_ACTION))
async def on_action_pick(
    callback: CallbackQuery, bot: Bot, _: Callable[..., str], state: FSMContext, lang: str = "fa"
) -> None:
    parsed = _parse_action_cb(callback.data or "", CB_ACTION)
    await callback.answer()
    if parsed is None:
        return
    action_type, service_id, product_id = parsed
    await _start_action_payment(
        callback.message, callback.from_user, action_type, service_id, product_id, _, state, bot)


@router.message(Command("coupons"))
@router.message(F.text.in_(menu_texts("btn.coupons")))
async def on_coupons(
    message: Message, _: Callable[..., str], state: FSMContext, lang: str = "fa"
) -> None:
    """List public active coupons, if the operator enabled that."""
    await state.clear()
    async with SessionLocal() as session:
        svc = SettingsService(session)
        if not (await svc.get_bool("coupons_enabled", True)
                and await svc.get_bool("show_public_coupons", False)):
            await message.answer(_("coupon.public.disabled"))
            return
        coupons = await coupon_service.list_public_coupons(session)
    if not coupons:
        await message.answer(_("coupon.public.empty"))
        return
    lines = [_("coupon.public.title"), ""]
    for c in coupons:
        if c.discount_type == "percent":
            value = _("coupon.public.percent", pct=c.discount_value)
        else:
            value = _("coupon.public.fixed", amount=f"{int(c.discount_value):,}")
        lines.append(_("coupon.public.row", code=c.code, value=value,
                       title=(c.title or "")))
    await message.answer("\n".join(lines), parse_mode="HTML")


async def _download_telegram_file(bot: Bot, file_id: str) -> bytes:
    """Download a Telegram file into memory. Isolated so tests can monkeypatch it."""
    tg_file = await bot.get_file(file_id)
    buf = BytesIO()
    await bot.download_file(tg_file.file_path, destination=buf)
    return buf.getvalue()


def _extract_file(message: Message) -> tuple[str, str, str | None, int] | None:
    """(file_id, original_name, mime, size) for a photo/document, or None."""
    if message.photo:
        photo = message.photo[-1]  # largest size
        return photo.file_id, "receipt.jpg", "image/jpeg", photo.file_size or 0
    if message.document:
        doc = message.document
        return doc.file_id, (doc.file_name or "receipt"), doc.mime_type, (doc.file_size or 0)
    return None


async def _handle_receipt(
    message: Message, bot: Bot, _: Callable[..., str], state: FSMContext,
    lang: str, order_id: int | None,
) -> None:
    extracted = _extract_file(message)
    if extracted is None:
        return
    file_id, original_name, mime, size = extracted

    # Cheap pre-download rejection for wrong type / obviously-too-big files.
    try:
        payment_service.precheck_receipt(original_name, size, mime)
    except ReceiptError as exc:
        await message.answer(_receipt_error_text(exc, _))
        return

    tg_user = message.from_user
    async with SessionLocal() as session:
        user = await user_service.get_by_telegram_id(session, tg_user.id)
        if user is None:
            await message.answer(_("purchase.no_pending_order"))
            return
        resolved_order_id = order_id
        if resolved_order_id is None:
            pending = await order_service.latest_pending_order(session, user.id)
            if pending is None:
                await message.answer(_("purchase.no_pending_order"))
                return
            resolved_order_id = pending.id

        try:
            content = await _download_telegram_file(bot, file_id)
        except Exception as exc:  # noqa: BLE001 - network/telegram error
            log.warning("Receipt download failed: %s", exc)
            await message.answer(_("purchase.download_failed"))
            return

        file_info = ReceiptFile(
            content=content, original_name=original_name, mime_type=mime, file_id=file_id
        )
        try:
            payment = await payment_service.submit_receipt(
                session, resolved_order_id, user.id, file_info
            )
            await session.commit()
        except ReceiptError as exc:
            await message.answer(_receipt_error_text(exc, _))
            return

        order = await order_service.get_order(session, resolved_order_id)
        product = order.product

    await state.clear()
    await message.answer(_("purchase.receipt_saved"))
    if payment.tracking_code:
        await message.answer(_(
            "purchase.receipt_waiting",
            amount=f"{payment.amount:,}", code=payment.tracking_code,
        ))
    # Best-effort financial log (never blocks the flow).
    try:
        from app.services import financial_log_service
        async with SessionLocal() as session:
            await financial_log_service.log_receipt_submitted(
                session, bot, tracking_code=payment.tracking_code or "-",
                amount=int(payment.amount or 0),
                user_label=f"@{tg_user.username}" if tg_user.username else str(tg_user.id),
            )
    except Exception as exc:  # noqa: BLE001
        log.warning("financial log failed: %s", exc)

    # Best-effort admin notification (never breaks the user's flow).
    try:
        from app.bot.notifications import notify_receipt_submitted

        await notify_receipt_submitted(
            bot, order=order, payment=payment, product=product, user=user, lang=lang
        )
    except Exception as exc:  # noqa: BLE001
        log.warning("Admin receipt notification failed: %s", exc)


@router.message(PurchaseStates.waiting_for_receipt, F.photo | F.document)
async def on_receipt_in_state(
    message: Message, bot: Bot, _: Callable[..., str], state: FSMContext, lang: str = "fa"
) -> None:
    data = await state.get_data()
    await _handle_receipt(message, bot, _, state, lang, data.get("order_id"))


@router.message(F.photo | F.document)
async def on_receipt_stateless(
    message: Message, bot: Bot, _: Callable[..., str], state: FSMContext, lang: str = "fa"
) -> None:
    # A receipt sent without an active buy flow: try the latest pending order.
    await _handle_receipt(message, bot, _, state, lang, None)


@router.message(
    PurchaseStates.waiting_for_receipt,
    F.text,
    ~F.text.startswith("/"),
    ~F.text.in_(_NAV_TEXTS),
)
async def on_receipt_wrong_type(message: Message, _: Callable[..., str]) -> None:
    await message.answer(_("purchase.receipt_required_file"))


@router.callback_query(F.data == "receipt_next_phase")
async def on_receipt_next_phase(
    callback: CallbackQuery, _: Callable[..., str], lang: str = "fa"
) -> None:
    """Placeholder for the disabled approve/reject button (arrives in Phase 4)."""
    await callback.answer(_("notify.receipt.next_phase"), show_alert=True)


# --------------------------------------------------------------------------
# «سفارش‌های من» — paginated list + per-order detail.
# --------------------------------------------------------------------------
PAGE_SIZE = 5
CB_ORDER_PAGE = "uordpg:"   # list page N
CB_ORDER_DETAIL = "uord:"   # order detail: <id>:<page>
CB_LIST_CLOSE = "ulistx"    # close an inline list (back to the reply menu)


def _clamp_page(page: int, total: int) -> tuple[int, int]:
    pages = max(1, (total + PAGE_SIZE - 1) // PAGE_SIZE)
    return max(0, min(page, pages - 1)), pages


async def _edit_or_send(callback: CallbackQuery, text: str, kb: InlineKeyboardMarkup) -> None:
    """Edit the inline message in place; fall back to a fresh message if edit fails."""
    msg = callback.message
    if msg is None:
        return
    try:
        await msg.edit_text(text, parse_mode="HTML", reply_markup=kb)
    except Exception:  # noqa: BLE001 - message unchanged / too old / not editable
        await msg.answer(text, parse_mode="HTML", reply_markup=kb)


def _order_summary(o, index: int, lang: str, _: Callable[..., str]) -> str:
    title = o.product.title if o.product else "—"
    return "\n".join([
        _("orders.row.item", index=index, number=o.order_number, title=title),
        _("orders.row.status", status=order_status_label(o.status, lang)),
        _("orders.row.amount", amount=f"{o.final_amount:,}"),
    ])


async def _build_orders_page(session, user_id: int, page: int, lang: str,
                             _: Callable[..., str]) -> tuple[str, InlineKeyboardMarkup]:
    total = await order_service.count_user_orders(session, user_id)
    page, pages = _clamp_page(page, total)
    orders = await order_service.list_user_orders(
        session, user_id, limit=PAGE_SIZE, offset=page * PAGE_SIZE)

    lines = [_("orders.user.title"), _("orders.page_of", page=page + 1, pages=pages), ""]
    detail_buttons: list[InlineKeyboardButton] = []
    for i, o in enumerate(orders):
        index = page * PAGE_SIZE + i + 1
        lines.append(_order_summary(o, index, lang, _))
        lines.append("")
        detail_buttons.append(InlineKeyboardButton(
            text=_("orders.btn.detail", index=index),
            callback_data=f"{CB_ORDER_DETAIL}{o.id}:{page}"))

    rows: list[list[InlineKeyboardButton]] = [detail_buttons[i:i + 2]
                                              for i in range(0, len(detail_buttons), 2)]
    nav: list[InlineKeyboardButton] = []
    if page > 0:
        nav.append(InlineKeyboardButton(text=_("btn.prev"),
                                        callback_data=f"{CB_ORDER_PAGE}{page - 1}"))
    if page < pages - 1:
        nav.append(InlineKeyboardButton(text=_("btn.next"),
                                        callback_data=f"{CB_ORDER_PAGE}{page + 1}"))
    if nav:
        rows.append(nav)
    rows.append([InlineKeyboardButton(text=_("btn.back"), callback_data=CB_LIST_CLOSE)])
    return "\n".join(lines).rstrip(), InlineKeyboardMarkup(inline_keyboard=rows)


async def _order_detail_text(session, o, lang: str, _: Callable[..., str]) -> str:
    from app.services import product_category_service
    title = o.product.title if o.product else "—"
    method_key = f"order.method.{o.payment_method}"
    method = _(method_key)
    if method == method_key:
        method = _("order.method.unknown")
    lines = [
        _("orders.detail.title"), "",
        _("orders.row.number", number=o.order_number),
        _("orders.row.product", title=title),
    ]
    if o.product is not None and o.product.category_id:
        cat = await product_category_service.get(session, o.product.category_id)
        if cat is not None:
            lines.append(_("orders.row.category", category=cat.title))
    lines.append(_("orders.row.status", status=order_status_label(o.status, lang)))
    lines.append(_("orders.row.method", method=method))
    if o.discount_amount:
        lines.append(_("orders.row.original", amount=f"{o.amount:,}"))
        lines.append(_("orders.row.discount", amount=f"{o.discount_amount:,}"))
    lines.append(_("orders.row.amount", amount=f"{o.final_amount:,}"))
    if o.created_at:
        lines.append(_("orders.row.date", date=o.created_at.strftime("%Y-%m-%d %H:%M")))
    if o.paid_at:
        lines.append(_("orders.row.paid_at", date=o.paid_at.strftime("%Y-%m-%d %H:%M")))
    if o.delivered_at:
        lines.append(_("orders.row.delivered_at", date=o.delivered_at.strftime("%Y-%m-%d %H:%M")))
    if o.status == "rejected" and o.reject_reason:
        lines.append(_("orders.row.reject", reason=o.reject_reason))
    delivery_key = "order.delivery.delivered" if o.delivered_at else "order.delivery.pending"
    lines.append(_("orders.row.delivery", delivery=_(delivery_key)))
    return "\n".join(lines)


async def render_orders(reply, tg_user, _: Callable[..., str], lang: str) -> None:
    """Render the caller's orders (page 1). Shared by /orders and the account page."""
    async with SessionLocal() as session:
        user = await user_service.get_by_telegram_id(session, tg_user.id)
        if user is None or await order_service.count_user_orders(session, user.id) == 0:
            await reply.answer(_("orders.user.empty"))
            return
        text, kb = await _build_orders_page(session, user.id, 0, lang, _)
    await reply.answer(text, parse_mode="HTML", reply_markup=kb)


@router.message(Command("orders"))
@router.message(F.text.in_(menu_texts("btn.my_orders")))
async def on_orders(
    message: Message, _: Callable[..., str], state: FSMContext, lang: str = "fa"
) -> None:
    await state.clear()
    await render_orders(message, message.from_user, _, lang)


@router.callback_query(F.data.startswith(CB_ORDER_PAGE))
async def on_orders_page(
    callback: CallbackQuery, _: Callable[..., str], lang: str = "fa"
) -> None:
    page = int((callback.data or "0")[len(CB_ORDER_PAGE):] or 0)
    async with SessionLocal() as session:
        user = await user_service.get_by_telegram_id(session, callback.from_user.id)
        if user is None:
            await callback.answer(_("orders.user.empty"), show_alert=True)
            return
        text, kb = await _build_orders_page(session, user.id, page, lang, _)
    await _edit_or_send(callback, text, kb)
    await callback.answer()


@router.callback_query(F.data.startswith(CB_ORDER_DETAIL))
async def on_order_detail(
    callback: CallbackQuery, _: Callable[..., str], lang: str = "fa"
) -> None:
    raw = (callback.data or "")[len(CB_ORDER_DETAIL):]
    order_id_s, _sep, page_s = raw.partition(":")
    try:
        order_id, page = int(order_id_s), int(page_s or 0)
    except ValueError:
        await callback.answer()
        return
    async with SessionLocal() as session:
        user = await user_service.get_by_telegram_id(session, callback.from_user.id)
        order = await order_service.get_order(session, order_id)
        # Only ever show the caller's own order.
        if user is None or order is None or order.user_id != user.id:
            await callback.answer(_("orders.detail.not_found"), show_alert=True)
            return
        text = await _order_detail_text(session, order, lang, _)
    kb = InlineKeyboardMarkup(inline_keyboard=[[
        InlineKeyboardButton(text=_("btn.back"), callback_data=f"{CB_ORDER_PAGE}{page}")]])
    await _edit_or_send(callback, text, kb)
    await callback.answer()


# --------------------------------------------------------------------------
# «لایسنس‌های من» — configurable-title paginated list + per-item detail.
# --------------------------------------------------------------------------
CB_LIC_PAGE = "ulicpg:"   # license list page N


class LicenseButtonFilter(BaseFilter):
    """Match the license main-menu button whose label is configurable.

    Matches the default i18n texts with no DB read; only when the text is not a
    default label does it read the configured ``license_section_title`` setting.
    """

    async def __call__(self, message: Message) -> bool:
        text = (message.text or "").strip()
        if not text:
            return False
        if text in menu_texts("btn.my_licenses"):
            return True
        async with SessionLocal() as session:
            title = (await SettingsService(session)
                     .get_str("license_section_title", "")).strip()
        return bool(title) and text == title


async def _license_section_title(session, _: Callable[..., str]) -> str:
    return (await SettingsService(session)
            .get_str("license_section_title", "")).strip() or _("licenses.user.title")


async def _build_licenses_page(session, user_id: int, page: int,
                               _: Callable[..., str]) -> tuple[str, InlineKeyboardMarkup]:
    title_text = await _license_section_title(session, _)
    total = await license_service.count_user_licenses(session, user_id)
    page, pages = _clamp_page(page, total)
    licenses = await license_service.list_user_licenses(
        session, user_id, limit=PAGE_SIZE, offset=page * PAGE_SIZE)

    # Resolve order numbers for this page (order_id -> order_number).
    order_ids = [lic.order_id for lic in licenses if lic.order_id]
    numbers: dict[int, str] = {}
    for oid in order_ids:
        o = await order_service.get_order(session, oid)
        if o is not None:
            numbers[oid] = o.order_number

    lines = [title_text, _("orders.page_of", page=page + 1, pages=pages), "",
             _("licenses.user.pick"), ""]
    rows: list[list[InlineKeyboardButton]] = []
    for i, lic in enumerate(licenses):
        index = page * PAGE_SIZE + i + 1
        prod = lic.product.title if lic.product else "—"
        number = numbers.get(lic.order_id or 0, "—")
        lines.append(_("licenses.row.item", index=index, number=number, title=prod))
        rows.append([InlineKeyboardButton(
            text=_("licenses.btn.item", index=index, number=number, title=prod)[:64],
            callback_data=f"{CB_LICENSE}{lic.id}:{page}")])

    nav: list[InlineKeyboardButton] = []
    if page > 0:
        nav.append(InlineKeyboardButton(text=_("btn.prev"),
                                        callback_data=f"{CB_LIC_PAGE}{page - 1}"))
    if page < pages - 1:
        nav.append(InlineKeyboardButton(text=_("btn.next"),
                                        callback_data=f"{CB_LIC_PAGE}{page + 1}"))
    if nav:
        rows.append(nav)
    rows.append([InlineKeyboardButton(text=_("btn.back"), callback_data=CB_LIST_CLOSE)])
    return "\n".join(lines).rstrip(), InlineKeyboardMarkup(inline_keyboard=rows)


def _license_detail_text(order, lic, lang: str, _: Callable[..., str]) -> str:
    number = order.order_number if order is not None else "—"
    product = lic.product.title if lic.product else "—"
    sold = lic.sold_at.strftime("%Y-%m-%d") if lic.sold_at else "—"
    lines = [
        _("licenses.detail.title"), "",
        _("licenses.detail.order", number=number),
        _("licenses.detail.product", title=product),
        _("licenses.detail.date", date=sold),
        _("licenses.detail.status", status=_("order.delivery.delivered")),
        "",
        _("license.delivery.email_label"), f"<code>{lic.email}</code>",
        "",
        _("license.delivery.password_label"), f"<code>{lic.password}</code>",
    ]
    if lic.note:
        lines += ["", _("license.delivery.note_label"), lic.note]
    lines += ["", _("license.delivery.keep_safe")]
    return "\n".join(lines)


async def render_my_licenses(reply, tg_user, _: Callable[..., str], lang: str) -> None:
    """Render the caller's licenses (page 1) using the configurable section title."""
    async with SessionLocal() as session:
        user = await user_service.get_by_telegram_id(session, tg_user.id)
        title_text = await _license_section_title(session, _)
        if user is None or await license_service.count_user_licenses(session, user.id) == 0:
            await reply.answer(f"{title_text}\n\n{_('licenses.user.empty')}")
            return
        text, kb = await _build_licenses_page(session, user.id, 0, _)
    await reply.answer(text, parse_mode="HTML", reply_markup=kb)


@router.message(Command("my_licenses"))
@router.message(F.text.in_(menu_texts("btn.my_licenses")))
@router.message(LicenseButtonFilter())
async def on_my_licenses(
    message: Message, _: Callable[..., str], state: FSMContext, lang: str = "fa"
) -> None:
    await state.clear()
    await render_my_licenses(message, message.from_user, _, lang)


@router.callback_query(F.data.startswith(CB_LIC_PAGE))
async def on_licenses_page(
    callback: CallbackQuery, _: Callable[..., str], lang: str = "fa"
) -> None:
    page = int((callback.data or "0")[len(CB_LIC_PAGE):] or 0)
    async with SessionLocal() as session:
        user = await user_service.get_by_telegram_id(session, callback.from_user.id)
        if user is None:
            await callback.answer(_("licenses.user.empty"), show_alert=True)
            return
        text, kb = await _build_licenses_page(session, user.id, page, _)
    await _edit_or_send(callback, text, kb)
    await callback.answer()


@router.callback_query(F.data.startswith(CB_LICENSE))
async def on_license_detail(
    callback: CallbackQuery, _: Callable[..., str], lang: str = "fa"
) -> None:
    raw = (callback.data or "")[len(CB_LICENSE):]
    lic_id_s, _sep, page_s = raw.partition(":")
    try:
        license_id, page = int(lic_id_s), int(page_s or 0)
    except ValueError:
        await callback.answer()
        return
    async with SessionLocal() as session:
        user = await user_service.get_by_telegram_id(session, callback.from_user.id)
        lic = await license_service.get_license(session, license_id)
        # Never reveal a license that is not this user's.
        if user is None or lic is None or lic.sold_to_user_id != user.id:
            await callback.answer(_("licenses.user.not_found"), show_alert=True)
            return
        order = await order_service.get_order(session, lic.order_id) if lic.order_id else None
        text = _license_detail_text(order, lic, lang, _)
    kb = InlineKeyboardMarkup(inline_keyboard=[[
        InlineKeyboardButton(text=_("btn.back"), callback_data=f"{CB_LIC_PAGE}{page}")]])
    await _edit_or_send(callback, text, kb)
    await callback.answer()


@router.callback_query(F.data == CB_LIST_CLOSE)
async def on_list_close(
    callback: CallbackQuery, _: Callable[..., str], lang: str = "fa"
) -> None:
    if callback.message is not None:
        try:
            await callback.message.delete()
        except Exception:  # noqa: BLE001 - deleting an old message may fail
            pass
    await callback.answer()


# --------------------------------------------------------------------------
# Payment Core: card-flow helper buttons. Telegram can't put text on the
# clipboard from an ordinary inline button, so "copy" answers with the value
# in a popup the user can long-press-copy from.
# --------------------------------------------------------------------------
@router.callback_query(F.data == "paycpa")
async def on_copy_amount(callback: CallbackQuery, _: Callable[..., str],
                         state: FSMContext) -> None:
    data = await state.get_data()
    amount = int(data.get("pay_amount") or 0)
    await callback.answer(_("purchase.copy_amount_popup", amount=f"{amount:,}"),
                          show_alert=True)


@router.callback_query(F.data == "paycpc")
async def on_copy_card(callback: CallbackQuery, _: Callable[..., str],
                       state: FSMContext) -> None:
    data = await state.get_data()
    card = str(data.get("card_number") or "")
    await callback.answer(_("purchase.copy_card_popup", card=card), show_alert=True)


@router.callback_query(F.data == "paydone")
async def on_paid_pressed(callback: CallbackQuery, _: Callable[..., str],
                          state: FSMContext) -> None:
    """«پرداخت کردم» — the receipt state is already armed; prompt for the file."""
    await state.set_state(PurchaseStates.waiting_for_receipt)
    if callback.message is not None:
        await callback.message.answer(_("purchase.send_receipt_now"))
    await callback.answer()

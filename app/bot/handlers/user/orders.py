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
from aiogram.filters import Command
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
from app.i18n import t, texts_for
from app.services import (
    license_service,
    order_service,
    payment_service,
    product_service,
    user_service,
    wallet_service,
)
from app.services.order_service import OrderError
from app.services.payment_service import ReceiptError, ReceiptFile

CB_LICENSE = "ulic:"

log = logging.getLogger("bot.user.orders")

router = Router(name="user.orders")

CB_BUY = "ubuy:"


class PurchaseStates(StatesGroup):
    waiting_for_receipt = State()


# Menu buttons/commands that must never be swallowed by the receipt-wait guidance.
_NAV_TEXTS: set[str] = set()
for _key in ("btn.products", "btn.account", "btn.support", "btn.rules",
             "btn.language", "btn.admin_panel", "btn.my_orders", "btn.my_licenses",
             "btn.my_services"):
    _NAV_TEXTS |= texts_for(_key)


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


async def _start_card_payment(reply, tg_user, product_id: int, _: Callable[..., str],
                              state: FSMContext, *, action_type: str | None = None,
                              target_service_id: int | None = None) -> None:
    """Create the card-to-card order + payment and show the transfer instructions.

    Extracted so the payment-method picker can reuse it. A renew/add-traffic order
    (Phase 8) passes ``action_type`` + ``target_service_id`` through to create_order.
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
            await payment_service.create_payment_for_order(session, order)
            await session.commit()
        except OrderError as exc:
            if reply is not None:
                await reply.answer(_order_error_text(exc, _))
            return
        product = await product_service.get(session, product_id)
        lines = _payment_instruction_lines(order, product, cfg, _)
        order_id = order.id

    await state.set_state(PurchaseStates.waiting_for_receipt)
    await state.update_data(order_id=order_id)
    if reply is not None:
        await reply.answer("\n".join(lines), parse_mode="HTML")


def _topup_button(_: Callable[..., str]) -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text=_("wallet.btn.topup"), callback_data="wtopup")],
    ])


async def _pay_with_wallet(reply, tg_user, product_id: int, _: Callable[..., str], bot,
                           *, action_type: str | None = None,
                           target_service_id: int | None = None) -> None:
    """Charge the wallet for the product, then run the existing delivery flow.

    A renew/add-traffic order (Phase 8) passes ``action_type`` +
    ``target_service_id`` through to create_order; delivery then routes to the
    lifecycle service, which sends its own action-specific confirmation."""
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
        price = int(product.price or 0)
        balance = await wallet_service.get_balance(session, user.id)
        if balance < price:
            if reply is not None:
                await reply.answer(
                    _("purchase.wallet.insufficient", balance=f"{balance:,}", amount=f"{price:,}"),
                    reply_markup=_topup_button(_))
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
        try:
            result = await wallet_service.pay_order_with_wallet(
                session, order.id, user.id, bot=bot)
            await session.commit()
        except wallet_service.InsufficientBalanceError:
            await session.rollback()
            bal = await wallet_service.get_balance(session, user.id)
            if reply is not None:
                await reply.answer(
                    _("purchase.wallet.insufficient", balance=f"{bal:,}", amount=f"{price:,}"),
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


@router.callback_query(F.data.startswith(CB_BUY))
async def on_buy(
    callback: CallbackQuery, bot: Bot, _: Callable[..., str], state: FSMContext, lang: str = "fa"
) -> None:
    """Offer the payment-method picker, or go straight to the only enabled method."""
    product_id = int((callback.data or "0")[len(CB_BUY):])
    reply = callback.message
    async with SessionLocal() as session:
        svc = SettingsService(session)
        if not await svc.get_bool("sales_enabled", True):
            await callback.answer(_("purchase.sales_disabled"), show_alert=True)
            return
        card_ok = (await svc.get_bool("card_to_card_enabled", True)
                   and bool((await svc.get_str("card_number", "")).strip()))
        wallet_ok = (await svc.get_bool("wallet_enabled", True)
                     and await svc.get_bool("wallet_payment_enabled", True))
    await callback.answer()
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
        await _pay_with_wallet(reply, callback.from_user, product_id, _, bot)
        return
    await _start_card_payment(reply, callback.from_user, product_id, _, state)


@router.callback_query(F.data.startswith(CB_PAY_CARD))
async def on_pay_card(
    callback: CallbackQuery, _: Callable[..., str], state: FSMContext, lang: str = "fa"
) -> None:
    product_id = int((callback.data or "0")[len(CB_PAY_CARD):])
    await callback.answer()
    await _start_card_payment(callback.message, callback.from_user, product_id, _, state)


@router.callback_query(F.data.startswith(CB_PAY_WALLET))
async def on_pay_wallet(
    callback: CallbackQuery, bot: Bot, _: Callable[..., str], lang: str = "fa"
) -> None:
    product_id = int((callback.data or "0")[len(CB_PAY_WALLET):])
    await callback.answer()
    await _pay_with_wallet(callback.message, callback.from_user, product_id, _, bot)


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


@router.message(Command("orders"))
@router.message(F.text.in_(texts_for("btn.my_orders")))
async def on_orders(
    message: Message, _: Callable[..., str], state: FSMContext, lang: str = "fa"
) -> None:
    await state.clear()
    tg_user = message.from_user
    async with SessionLocal() as session:
        user = await user_service.get_by_telegram_id(session, tg_user.id)
        orders = await order_service.list_user_orders(session, user.id) if user else []

    if not orders:
        await message.answer(_("orders.user.empty"))
        return

    lines = [_("orders.user.title"), ""]
    for o in orders:
        title = o.product.title if o.product else "—"
        created = o.created_at.strftime("%Y-%m-%d") if o.created_at else ""
        lines.append(
            f"• <code>{o.order_number}</code> · {title} · "
            f"{o.final_amount:,} · {order_status_label(o.status, lang)} · {created}"
        )
    await message.answer("\n".join(lines), parse_mode="HTML")


@router.message(Command("my_licenses"))
@router.message(F.text.in_(texts_for("btn.my_licenses")))
async def on_my_licenses(
    message: Message, _: Callable[..., str], state: FSMContext, lang: str = "fa"
) -> None:
    await state.clear()
    tg_user = message.from_user
    async with SessionLocal() as session:
        user = await user_service.get_by_telegram_id(session, tg_user.id)
        licenses = await license_service.list_user_licenses(session, user.id) if user else []

    if not licenses:
        await message.answer(_("licenses.user.empty"))
        return

    lines = [_("licenses.user.title"), ""]
    buttons: list[list[InlineKeyboardButton]] = []
    for lic in licenses:
        title = lic.product.title if lic.product else "—"
        sold = lic.sold_at.strftime("%Y-%m-%d") if lic.sold_at else ""
        order_no = f" · #{lic.order_id}" if lic.order_id else ""
        lines.append(f"• {title}{order_no} · {sold}")
        buttons.append([InlineKeyboardButton(
            text=title, callback_data=f"{CB_LICENSE}{lic.id}")])
    await message.answer(
        "\n".join(lines), parse_mode="HTML",
        reply_markup=InlineKeyboardMarkup(inline_keyboard=buttons),
    )


@router.callback_query(F.data.startswith(CB_LICENSE))
async def on_license_detail(
    callback: CallbackQuery, _: Callable[..., str], lang: str = "fa"
) -> None:
    license_id = int((callback.data or "0")[len(CB_LICENSE):])
    tg_user = callback.from_user
    text: str | None = None
    async with SessionLocal() as session:
        user = await user_service.get_by_telegram_id(session, tg_user.id)
        lic = await license_service.get_license(session, license_id)
        # Never reveal a license that is not this user's.
        if user is None or lic is None or lic.sold_to_user_id != user.id:
            await callback.answer(_("licenses.user.not_found"), show_alert=True)
            return
        order = await order_service.get_order(session, lic.order_id) if lic.order_id else None
        text = license_service.build_delivery_message(order, lic.product, lic, lang)
    if callback.message is not None and text:
        await callback.message.answer(text, parse_mode="HTML")
    await callback.answer()

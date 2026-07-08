"""Phase 7 user wallet: /wallet menu, card-to-card top-up (amount → receipt),
and transaction history. Wallet *payment* for an order lives in orders.py.
"""
from __future__ import annotations

import logging
from collections.abc import Callable

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

from app.bot.payment_ui import (
    UNIMPLEMENTED_GATEWAY_TYPES,
    manual_receipt_keyboard,
    method_label,
)
from app.core.settings_service import SettingsService
from app.core.statuses import wallet_tx_type_label
from app.database import SessionLocal
from app.i18n import menu_texts
from app.services import (
    payment_core_service,
    payment_service,
    user_service,
    wallet_service,
)
from app.services.wallet_service import WalletError

log = logging.getLogger("bot.user.wallet")

router = Router(name="user.wallet")

CB_TOPUP = "wtopup"
CB_HISTORY = "whist"
CB_TU_METHOD = "wtum:"   # top-up method chosen: wtum:<method_type>
CB_TU_PAID = "wtupaid"   # «پرداخت کردم» on the top-up card screen
CB_TU_BACK = "wtuback"   # «بازگشت» from the top-up card screen

# Menu buttons that must never be swallowed by the amount/receipt prompts.
_NAV_TEXTS: set[str] = set()
for _key in ("btn.products", "btn.account", "btn.support", "btn.rules", "btn.language",
             "btn.admin_panel", "btn.my_orders", "btn.my_licenses", "btn.my_services",
             "btn.wallet"):
    _NAV_TEXTS |= menu_texts(_key)


class WalletStates(StatesGroup):
    waiting_for_amount = State()
    waiting_for_topup_receipt = State()


def _card_cfg(cfg: dict) -> bool:
    return bool(cfg.get("card_number"))


def _topup_receipt_error_text(exc: "payment_service.ReceiptError", _: Callable[..., str]) -> str:
    """Map a ReceiptError.code to a specific Persian message, else a generic one."""
    key = f"wallet.topup.receipt.{exc.code}"
    text = _(key)
    return text if text != key else _("wallet.topup.receipt_rejected", error=str(exc))


async def _read_card_cfg(svc: SettingsService) -> dict[str, str]:
    return {
        "card_number": (await svc.get_str("card_number", "")).strip(),
        "card_owner": (await svc.get_str("card_owner", "")).strip(),
        "sheba_number": (await svc.get_str("sheba_number", "")).strip(),
        "payment_instructions": (await svc.get_str("payment_instructions", "")).strip(),
    }


def _wallet_menu_kb(_: Callable[..., str]) -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text=_("wallet.btn.topup"), callback_data=CB_TOPUP)],
        [InlineKeyboardButton(text=_("wallet.btn.history"), callback_data=CB_HISTORY)],
    ])


async def render_wallet(reply, tg_user, _: Callable[..., str]) -> None:
    """Show the wallet balance + menu. Shared by /wallet and the account page."""
    async with SessionLocal() as session:
        if not await wallet_service.wallet_enabled(session):
            await reply.answer(_("wallet.disabled"))
            return
        user = await user_service.get_by_telegram_id(session, tg_user.id)
        balance = int(user.wallet_balance or 0) if user else 0
    await reply.answer(
        f"{_('wallet.title')}\n\n{_('wallet.balance', amount=f'{balance:,}')}",
        parse_mode="HTML", reply_markup=_wallet_menu_kb(_),
    )


async def _show_wallet(message: Message, _: Callable[..., str]) -> None:
    await render_wallet(message, message.from_user, _)


@router.message(Command("wallet"))
@router.message(F.text.in_(menu_texts("btn.wallet")))
async def on_wallet(
    message: Message, _: Callable[..., str], state: FSMContext, lang: str = "fa"
) -> None:
    await state.clear()
    await _show_wallet(message, _)


@router.message(F.text.in_(menu_texts("btn.wallet_history")))
async def on_wallet_history_button(
    message: Message, _: Callable[..., str], state: FSMContext, lang: str = "fa"
) -> None:
    await state.clear()
    await _send_history(message, _, lang)


async def _send_history(message: Message, _: Callable[..., str], lang: str) -> None:
    tg = message.from_user
    async with SessionLocal() as session:
        user = await user_service.get_by_telegram_id(session, tg.id)
        txns = await wallet_service.list_transactions(session, user.id, limit=10) if user else []
    if not txns:
        await message.answer(_("wallet.history.empty"))
        return
    lines = [_("wallet.history.title"), ""]
    for tx in txns:
        when = tx.created_at.strftime("%Y-%m-%d") if tx.created_at else ""
        sign = "＋" if tx.amount >= 0 else "－"
        lines.append(_("wallet.history.row", when=when,
                       type=wallet_tx_type_label(tx.type, lang),
                       sign=sign, amount=f"{abs(tx.amount):,}",
                       balance=f"{tx.balance_after:,}"))
        if tx.reason:
            lines.append(f"   <i>{tx.reason}</i>")
    await message.answer("\n".join(lines), parse_mode="HTML")


@router.callback_query(F.data == CB_HISTORY)
async def on_wallet_history_cb(
    callback: CallbackQuery, _: Callable[..., str], lang: str = "fa"
) -> None:
    await callback.answer()
    if callback.message is not None:
        await _send_history(callback.message, _, lang)


# --- top-up flow ------------------------------------------------------------
async def _start_topup(message: Message, _: Callable[..., str], state: FSMContext) -> None:
    async with SessionLocal() as session:
        if not await wallet_service.topup_enabled(session):
            await message.answer(_("wallet.topup.disabled"))
            return
        cfg = await _read_card_cfg(SettingsService(session))
        svc = SettingsService(session)
        min_topup = await svc.get_int("min_wallet_topup", 0)
        max_topup = await svc.get_int("max_wallet_topup", 0)
    if not _card_cfg(cfg):
        await message.answer(_("wallet.topup.not_configured"))
        return
    hint = _("wallet.topup.ask_amount")
    if min_topup:
        hint += "\n" + _("wallet.topup.min", amount=f"{min_topup:,}")
    if max_topup:
        hint += "\n" + _("wallet.topup.max", amount=f"{max_topup:,}")
    await state.set_state(WalletStates.waiting_for_amount)
    await message.answer(hint)


@router.callback_query(F.data == CB_TOPUP)
async def on_topup_cb(
    callback: CallbackQuery, _: Callable[..., str], state: FSMContext, lang: str = "fa"
) -> None:
    await callback.answer()
    if callback.message is not None:
        await _start_topup(callback.message, _, state)


@router.message(F.text.in_(menu_texts("btn.wallet_topup")))
async def on_topup_button(
    message: Message, _: Callable[..., str], state: FSMContext, lang: str = "fa"
) -> None:
    await state.clear()
    await _start_topup(message, _, state)


def _topup_instructions(amount: int, cfg: dict, _: Callable[..., str]) -> list[str]:
    lines = [
        _("wallet.topup.instructions_title"), "",
        _("wallet.topup.amount", amount=f"{amount:,}"), "",
        _("purchase.pay_header"),
        _("purchase.card_number", card=cfg["card_number"]),
    ]
    if cfg.get("card_owner"):
        lines.append(_("purchase.card_owner", owner=cfg["card_owner"]))
    if cfg.get("sheba_number"):
        lines.append(_("purchase.sheba", sheba=cfg["sheba_number"]))
    if cfg.get("payment_instructions"):
        lines.extend(["", cfg["payment_instructions"]])
    lines.extend(["", _("wallet.topup.ask_receipt")])
    return lines


@router.message(
    WalletStates.waiting_for_amount, F.text,
    ~F.text.startswith("/"), ~F.text.in_(_NAV_TEXTS),
)
async def on_topup_amount(
    message: Message, _: Callable[..., str], state: FSMContext, lang: str = "fa"
) -> None:
    raw = (message.text or "").strip().replace(",", "").replace("،", "")
    if not raw.isdigit():
        await message.answer(_("wallet.topup.bad_amount"))
        return
    amount = int(raw)
    tg = message.from_user
    async with SessionLocal() as session:
        user, _c = await user_service.create_or_update_from_telegram(
            session, telegram_id=tg.id, username=tg.username,
            first_name=tg.first_name, last_name=tg.last_name)
        await session.commit()
        try:
            topup = await wallet_service.create_topup_request(session, user.id, amount)
            await session.commit()
        except WalletError as exc:
            await message.answer(_("wallet.topup.error", error=str(exc)))
            return
        cfg = await _read_card_cfg(SettingsService(session))
        # All active methods for a top-up (a top-up can't be paid FROM the wallet).
        types = await payment_core_service.resolve_method_types(
            session, amount, user, exclude_wallet=True)
    # Remember the request + amount so the method/copy callbacks can read them.
    await state.set_state(None)
    await state.update_data(topup_id=topup.id, topup_amount=amount,
                            pay_amount=amount, card_number=cfg["card_number"])

    # Only manual receipt available (the common default) → skip the extra tap.
    if not types or types == ["manual_receipt"]:
        await _show_topup_card(message, amount, topup.id, cfg, _, state)
        return
    rows = [[InlineKeyboardButton(text=method_label(_, t), callback_data=f"{CB_TU_METHOD}{t}")]
            for t in types]
    await message.answer(_("purchase.choose_method"),
                         reply_markup=InlineKeyboardMarkup(inline_keyboard=rows))


async def _show_topup_card(reply, amount: int, topup_id: int, cfg: dict,
                           _: Callable[..., str], state: FSMContext) -> None:
    """Show the card-to-card instructions + copy/paid/back buttons for a top-up."""
    await state.set_state(WalletStates.waiting_for_topup_receipt)
    await state.update_data(topup_id=topup_id, pay_amount=amount,
                            card_number=cfg["card_number"])
    kb = manual_receipt_keyboard(_, amount=amount, card_number=cfg["card_number"],
                                 paid_cb=CB_TU_PAID, back_cb=CB_TU_BACK)
    await reply.answer("\n".join(_topup_instructions(amount, cfg, _)),
                       parse_mode="HTML", reply_markup=kb)


@router.callback_query(F.data.startswith(CB_TU_METHOD))
async def on_topup_method(
    callback: CallbackQuery, _: Callable[..., str], state: FSMContext, lang: str = "fa"
) -> None:
    """A top-up payment method was chosen from the picker."""
    method_type = (callback.data or "")[len(CB_TU_METHOD):]
    data = await state.get_data()
    topup_id = data.get("topup_id")
    amount = int(data.get("topup_amount") or 0)
    if topup_id is None:
        await callback.answer(_("wallet.topup.no_pending"), show_alert=True)
        return
    if method_type in UNIMPLEMENTED_GATEWAY_TYPES:
        # Never dead-end: clear notice that the gateway isn't wired to a provider.
        await callback.answer(_("gateway.not_connected"), show_alert=True)
        return
    await callback.answer()
    async with SessionLocal() as session:
        cfg = await _read_card_cfg(SettingsService(session))
    if callback.message is not None:
        await _show_topup_card(callback.message, amount, topup_id, cfg, _, state)


@router.callback_query(F.data == CB_TU_PAID)
async def on_topup_paid(
    callback: CallbackQuery, _: Callable[..., str], state: FSMContext, lang: str = "fa"
) -> None:
    """«پرداخت کردم» — receipt state is armed; prompt for the file."""
    await state.set_state(WalletStates.waiting_for_topup_receipt)
    await callback.answer()
    if callback.message is not None:
        await callback.message.answer(_("wallet.topup.ask_receipt"))


@router.callback_query(F.data == CB_TU_BACK)
async def on_topup_back(
    callback: CallbackQuery, _: Callable[..., str], state: FSMContext, lang: str = "fa"
) -> None:
    """«بازگشت» from the top-up card screen: leave the receipt-wait state."""
    await state.clear()
    await callback.answer()
    if callback.message is not None:
        await render_wallet(callback.message, callback.from_user, _)


@router.message(WalletStates.waiting_for_amount, F.text, ~F.text.in_(_NAV_TEXTS),
                ~F.text.startswith("/"))
async def on_topup_amount_wrong(message: Message, _: Callable[..., str]) -> None:
    await message.answer(_("wallet.topup.bad_amount"))


@router.message(WalletStates.waiting_for_topup_receipt, F.photo | F.document)
async def on_topup_receipt(
    message: Message, bot: Bot, _: Callable[..., str], state: FSMContext, lang: str = "fa"
) -> None:
    """Robust wrapper: any unexpected failure still replies (never stuck)."""
    try:
        await _handle_topup_receipt(message, bot, _, state, lang)
    except Exception as exc:  # noqa: BLE001 - a receipt must never dead-end the user
        log.exception("Unexpected top-up receipt error: %s", exc)
        try:
            await message.answer(_("wallet.topup.receipt_rejected", error="internal"))
        except Exception:  # noqa: BLE001
            pass


async def _handle_topup_receipt(
    message: Message, bot: Bot, _: Callable[..., str], state: FSMContext, lang: str = "fa"
) -> None:
    from app.bot.handlers.user.orders import _download_telegram_file, _extract_file
    data = await state.get_data()
    topup_id = data.get("topup_id")
    extracted = _extract_file(message)
    if extracted is None:
        return
    file_id, original_name, mime, size = extracted
    try:
        payment_service.precheck_receipt(original_name, size, mime)
    except payment_service.ReceiptError as exc:
        await message.answer(_topup_receipt_error_text(exc, _))
        return
    tg = message.from_user
    async with SessionLocal() as session:
        user = await user_service.get_by_telegram_id(session, tg.id)
        if user is None or topup_id is None:
            await message.answer(_("wallet.topup.no_pending"))
            return
        try:
            content = await _download_telegram_file(bot, file_id)
        except Exception as exc:  # noqa: BLE001
            log.warning("Top-up receipt download failed: %s", exc)
            await message.answer(_("purchase.download_failed"))
            return
        fi = payment_service.ReceiptFile(content=content, original_name=original_name,
                                         mime_type=mime, file_id=file_id)
        try:
            topup = await wallet_service.submit_topup_receipt(session, topup_id, user.id, fi)
            await session.commit()
        except payment_service.ReceiptError as exc:
            await message.answer(_topup_receipt_error_text(exc, _))
            return
        except WalletError as exc:
            await message.answer(_("wallet.topup.receipt_rejected", error=str(exc)))
            return
        amount = int(topup.amount or 0)
        topup_num = topup.id
    # Move the FSM forward immediately so the user is never left waiting.
    await state.clear()
    await message.answer(
        _("wallet.topup.receipt_saved_detail", amount=f"{amount:,}", topup_id=topup_num))
    # Best-effort admin notification.
    try:
        from app.bot.notifications import notify_wallet_topup_submitted
        await notify_wallet_topup_submitted(bot, topup=topup, user=user, lang=lang)
    except Exception as exc:  # noqa: BLE001
        log.warning("Wallet top-up admin notification failed: %s", exc)


@router.message(WalletStates.waiting_for_topup_receipt, F.text,
                ~F.text.startswith("/"), ~F.text.in_(_NAV_TEXTS))
async def on_topup_receipt_wrong(message: Message, _: Callable[..., str]) -> None:
    await message.answer(_("wallet.topup.receipt_required_file"))


@router.message(WalletStates.waiting_for_topup_receipt,
                ~F.text.startswith("/"), ~F.text.in_(_NAV_TEXTS))
async def on_topup_receipt_catch_all(message: Message, _: Callable[..., str]) -> None:
    """Anything else while waiting for a receipt (sticker, voice, contact, empty
    forward…): reply with guidance so the flow is never silently stuck.

    Commands and main-menu buttons are excluded so navigation still works — those
    fall through to their own routers (which may be registered after this one)."""
    await message.answer(_("wallet.topup.receipt_required_file"))

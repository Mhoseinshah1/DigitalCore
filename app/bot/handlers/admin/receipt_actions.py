"""Telegram admin quick actions on a submitted receipt (Phase 4).

Inline buttons on the receipt notification let an admin approve/reject the
receipt, add/subtract the user's wallet balance, block/restrict the user, view
the user, or open the panel. Multi-step actions (wallet amount+reason, reject
reason, restrict reason) run through a small FSM.

FSM safety: state carries order_id/user_id/admin_id/action; only the admin who
started an action can complete it; `/cancel` (or the لغو button) aborts; expired
or malformed callbacks fail safely; non-admins are refused.
"""
from __future__ import annotations

import logging
from collections.abc import Callable

from aiogram import F, Router
from aiogram.filters import Command
from aiogram.fsm.context import FSMContext
from aiogram.fsm.state import State, StatesGroup
from aiogram.types import (
    CallbackQuery,
    InlineKeyboardButton,
    InlineKeyboardMarkup,
    Message,
)

from app.core.permissions import Role, has_permission
from app.core.statuses import order_status_label
from app.database import SessionLocal
from app.services import (
    audit_service,
    order_service,
    payment_service,
    user_service,
    wallet_service,
)

log = logging.getLogger("bot.admin.receipt_actions")

router = Router(name="admin.receipt_actions")

CB = "radm:"  # radm:<action>:<order_id>

# action -> permission required (None = any admin).
_ACTION_PERM: dict[str, str | None] = {
    "approve": "process_payments",
    "reject": "process_payments",
    "details": "view_payments",
    "delrcpt": "process_payments",
    "addbal": "adjust_wallet",
    "subbal": "adjust_wallet",
    "block": "block_users",
    "blockok": "block_users",
    "restrict": "restrict_users",
    "viewuser": "view_users",
    "panel": None,
    "cancel": None,
}


class ReceiptActionStates(StatesGroup):
    waiting_amount = State()
    waiting_reason = State()


def receipt_action_keyboard(order_id: int, _: Callable[..., str]) -> InlineKeyboardMarkup:
    def b(action: str, key: str) -> InlineKeyboardButton:
        return InlineKeyboardButton(text=_(key), callback_data=f"{CB}{action}:{order_id}")
    return InlineKeyboardMarkup(inline_keyboard=[
        [b("approve", "notify.receipt.btn.approve"), b("reject", "notify.receipt.btn.reject")],
        [b("details", "notify.receipt.btn.details"), b("delrcpt", "notify.receipt.btn.delete")],
        [b("addbal", "notify.receipt.btn.addbal"), b("subbal", "notify.receipt.btn.subbal")],
        [b("block", "notify.receipt.btn.block"), b("restrict", "notify.receipt.btn.restrict")],
        [b("viewuser", "notify.receipt.btn.viewuser"), b("panel", "notify.receipt.btn.panel")],
    ])


def _cancel_kb(order_id: int, _: Callable[..., str]) -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text=_("radm.btn.cancel"), callback_data=f"{CB}cancel:{order_id}")],
    ])


def _parse(data: str | None) -> tuple[str, int] | None:
    parts = (data or "").split(":")
    if len(parts) != 3 or parts[0] != "radm":
        return None
    try:
        return parts[1], int(parts[2])
    except ValueError:
        return None


async def _answer(callback: CallbackQuery, text: str) -> None:
    if callback.message is not None:
        await callback.message.answer(text, parse_mode="HTML")
    await callback.answer()


@router.callback_query(F.data.startswith(CB))
async def on_receipt_action(
    callback: CallbackQuery, _: Callable[..., str], state: FSMContext,
    bot=None, role: Role | None = None, is_admin: bool = False, lang: str = "fa",
) -> None:
    parsed = _parse(callback.data)
    if parsed is None:
        await callback.answer()
        return
    action, order_id = parsed

    if not is_admin:
        await callback.answer(_("radm.not_authorized"), show_alert=True)
        return
    perm = _ACTION_PERM.get(action)
    if perm is not None and not has_permission(role, perm):
        await callback.answer(_("radm.not_authorized"), show_alert=True)
        return

    admin_tg = callback.from_user.id

    if action == "cancel":
        await state.clear()
        await callback.answer(_("radm.cancelled"), show_alert=False)
        return
    if action == "panel":
        await callback.answer(_("radm.panel_hint"), show_alert=True)
        return

    async with SessionLocal() as session:
        order = await order_service.get_order(session, order_id)
        if order is None:
            await callback.answer(_("radm.order_not_found"), show_alert=True)
            return
        user_id = order.user_id
        user = order.user
        number = order.order_number

        if action == "approve":
            try:
                result = await payment_service.approve_payment(
                    session, order_id, admin_id=None, bot=bot
                )
                await session.commit()
            except payment_service.ReceiptError as exc:
                await callback.answer(_(f"radm.err.{exc.code}") if _(f"radm.err.{exc.code}") != f"radm.err.{exc.code}"
                                      else _("radm.not_reviewable"), show_alert=True)
                return
            delivered = result.get("delivery", {}).get("delivered")
            await _answer(callback, _("radm.approved") if delivered else _("radm.approved_undelivered"))
            return

        if action == "viewuser":
            lines = [
                _("radm.viewuser.title"),
                _("radm.viewuser.user", username=(user.username or user.first_name or "—") if user else "—",
                  tg_id=(user.telegram_id if user else "—")),
                _("radm.viewuser.wallet", amount=f"{(user.wallet_balance if user else 0):,}"),
                _("radm.viewuser.flags",
                  blocked=("✅" if user and user.is_blocked else "—"),
                  restricted=("✅" if user and user.is_restricted else "—")),
                _("radm.viewuser.order", number=number, status=order_status_label(order.status, lang)),
            ]
            await _answer(callback, "\n".join(lines))
            return

        if action == "details":
            payment = await payment_service.get_payment_by_order(session, order_id)
            product = order.product
            method_key = f"order.method.{order.payment_method}"
            method = _(method_key)
            if method == method_key:
                method = _("order.method.unknown")
            full_name = " ".join(filter(None, [
                (user.first_name if user else None), (user.last_name if user else None)])) or "—"
            tracking = (payment.tracking_code if payment else None) or "—"
            submitted = (payment.submitted_at.strftime("%Y-%m-%d %H:%M")
                         if payment and payment.submitted_at else "—")
            lines = [
                _("radm.details.title"),
                _("radm.details.order", number=number, status=order_status_label(order.status, lang)),
                _("radm.details.product", title=(product.title if product else "—")),
                _("radm.details.amount", amount=f"{order.final_amount:,}"),
                _("radm.details.method", method=method),
                _("radm.details.tracking", code=tracking),
                _("radm.details.user",
                  name=full_name,
                  username=("@" + user.username) if user and user.username else "—",
                  tg_id=(user.telegram_id if user else "—")),
                _("radm.details.wallet", amount=f"{(user.wallet_balance if user else 0):,}"),
                _("radm.details.time", time=submitted),
            ]
            await _answer(callback, "\n".join(lines))
            return

        if action == "delrcpt":
            try:
                await payment_service.delete_receipt(session, order_id)
                await session.commit()
            except payment_service.ReceiptError:
                await callback.answer(_("radm.not_reviewable"), show_alert=True)
                return
            await audit_service.log(
                session, actor_type="admin", actor_id=admin_tg, action="receipt_deleted",
                target_type="order", target_id=order_id)
            await session.commit()
            await _answer(callback, _("radm.receipt_deleted"))
            return

        if action == "blockok":
            await user_service.admin_set_blocked(session, user_id, True, actor_id=admin_tg)
            await audit_service.log(
                session, actor_type="admin", actor_id=admin_tg,
                action="user_blocked_from_receipt_review", target_type="user",
                target_id=user_id, meta=f"order_id={order_id}",
            )
            await session.commit()
            await _answer(callback, _("radm.block_ok"))
            return

    # Actions that need input or confirmation.
    if action == "block":
        kb = InlineKeyboardMarkup(inline_keyboard=[
            [InlineKeyboardButton(text=_("radm.btn.confirm_block"),
                                  callback_data=f"{CB}blockok:{order_id}")],
            [InlineKeyboardButton(text=_("radm.btn.cancel"), callback_data=f"{CB}cancel:{order_id}")],
        ])
        if callback.message is not None:
            await callback.message.answer(_("radm.block_confirm"), reply_markup=kb)
        await callback.answer()
        return

    await state.set_state(
        ReceiptActionStates.waiting_amount if action in ("addbal", "subbal")
        else ReceiptActionStates.waiting_reason
    )
    await state.update_data(action=action, order_id=order_id, user_id=user_id, admin_id=admin_tg)
    prompt = {
        "addbal": "radm.ask_amount", "subbal": "radm.ask_amount",
        "reject": "radm.ask_reject_reason", "restrict": "radm.ask_restrict_reason",
    }[action]
    if callback.message is not None:
        await callback.message.answer(_(prompt), reply_markup=_cancel_kb(order_id, _))
    await callback.answer()


@router.message(Command("cancel"), ReceiptActionStates.waiting_amount)
@router.message(Command("cancel"), ReceiptActionStates.waiting_reason)
async def on_cancel(message: Message, _: Callable[..., str], state: FSMContext) -> None:
    await state.clear()
    await message.answer(_("radm.cancelled"))


@router.message(ReceiptActionStates.waiting_amount, F.text)
async def on_amount(
    message: Message, _: Callable[..., str], state: FSMContext
) -> None:
    data = await state.get_data()
    if message.from_user is None or message.from_user.id != data.get("admin_id"):
        return  # only the initiating admin may complete this action
    raw = (message.text or "").strip()
    try:
        amount = int(raw)
        if amount <= 0:
            raise ValueError
    except ValueError:
        await message.answer(_("radm.invalid_amount"))
        return
    await state.update_data(amount=amount)
    await state.set_state(ReceiptActionStates.waiting_reason)
    await message.answer(_("radm.ask_reason"),
                         reply_markup=_cancel_kb(int(data.get("order_id", 0)), _))


@router.message(ReceiptActionStates.waiting_reason, F.text)
async def on_reason(
    message: Message, _: Callable[..., str], state: FSMContext, lang: str = "fa"
) -> None:
    data = await state.get_data()
    if message.from_user is None or message.from_user.id != data.get("admin_id"):
        return
    reason = (message.text or "").strip()
    if not reason:
        await message.answer(_("radm.ask_reason"))
        return
    action = data.get("action")
    order_id = int(data.get("order_id", 0))
    user_id = int(data.get("user_id", 0))
    amount = int(data.get("amount", 0))
    admin_tg = data.get("admin_id")

    await state.clear()
    async with SessionLocal() as session:
        try:
            if action == "addbal":
                await wallet_service.add_balance(session, user_id, amount,
                                                 admin_id=admin_tg, reason=reason)
                await audit_service.log(
                    session, actor_type="admin", actor_id=admin_tg,
                    action="admin_wallet_added_from_receipt_review", target_type="user",
                    target_id=user_id, new=str(amount), meta=f"order_id={order_id}")
                msg = _("radm.wallet_added")
            elif action == "subbal":
                await wallet_service.subtract_balance(session, user_id, amount,
                                                      admin_id=admin_tg, reason=reason)
                await audit_service.log(
                    session, actor_type="admin", actor_id=admin_tg,
                    action="admin_wallet_subtracted_from_receipt_review", target_type="user",
                    target_id=user_id, new=str(amount), meta=f"order_id={order_id}")
                msg = _("radm.wallet_subtracted")
            elif action == "reject":
                await payment_service.reject_payment(session, order_id, admin_id=None, reason=reason)
                msg = _("radm.rejected_ok")
            elif action == "restrict":
                await user_service.set_restricted(session, user_id, True, reason=reason,
                                                  actor_id=admin_tg)
                await audit_service.log(
                    session, actor_type="admin", actor_id=admin_tg,
                    action="user_restricted_from_receipt_review", target_type="user",
                    target_id=user_id, meta=f"order_id={order_id} reason={reason}")
                msg = _("radm.restrict_ok")
            else:
                msg = _("radm.cancelled")
            await session.commit()
        except (ValueError, payment_service.ReceiptError) as exc:
            await message.answer(_("radm.error", error=str(exc)))
            return
    await message.answer(msg)

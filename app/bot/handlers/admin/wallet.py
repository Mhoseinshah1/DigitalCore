"""Telegram admin quick actions on a wallet top-up receipt (Phase 7).

Inline buttons on the top-up notification let a permitted admin approve or reject
(with a reason) a submitted top-up. Only admins with `manage_wallet_topups` may
act; only the admin who started a reject can finish it; the service enforces the
real idempotency (a second approve/reject fails safely).
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
from app.database import SessionLocal
from app.services import wallet_service
from app.services.wallet_service import WalletError

log = logging.getLogger("bot.admin.wallet")

router = Router(name="admin.wallet")

CB = "wadm:"  # wadm:<action>:<topup_id>
_ACTION_PERM: dict[str, str | None] = {
    "approve": "manage_wallet_topups",
    "reject": "manage_wallet_topups",
    "panel": None,
    "cancel": None,
}


class WalletAdminStates(StatesGroup):
    waiting_reason = State()


def topup_action_keyboard(topup_id: int, _: Callable[..., str]) -> InlineKeyboardMarkup:
    def b(action: str, key: str) -> InlineKeyboardButton:
        return InlineKeyboardButton(text=_(key), callback_data=f"{CB}{action}:{topup_id}")
    return InlineKeyboardMarkup(inline_keyboard=[
        [b("approve", "notify.topup.btn.approve"), b("reject", "notify.topup.btn.reject")],
        [b("panel", "notify.topup.btn.panel")],
    ])


def _parse(data: str | None) -> tuple[str, int] | None:
    parts = (data or "").split(":")
    if len(parts) != 3 or parts[0] != "wadm":
        return None
    try:
        return parts[1], int(parts[2])
    except ValueError:
        return None


@router.callback_query(F.data.startswith(CB))
async def on_topup_action(
    callback: CallbackQuery, _: Callable[..., str], state: FSMContext,
    bot=None, role: Role | None = None, is_admin: bool = False, lang: str = "fa",
) -> None:
    parsed = _parse(callback.data)
    if parsed is None:
        await callback.answer()
        return
    action, topup_id = parsed
    if not is_admin:
        await callback.answer(_("radm.not_authorized"), show_alert=True)
        return
    perm = _ACTION_PERM.get(action)
    if perm is not None and not has_permission(role, perm):
        await callback.answer(_("radm.not_authorized"), show_alert=True)
        return

    if action == "cancel":
        await state.clear()
        await callback.answer(_("radm.cancelled"))
        return
    if action == "panel":
        await callback.answer(_("radm.panel_hint"), show_alert=True)
        return
    if action == "approve":
        async with SessionLocal() as session:
            try:
                r = await wallet_service.approve_topup(session, topup_id, admin_id=None, bot=bot)
                await session.commit()
            except WalletError:
                await callback.answer(_("notify.topup.err"), show_alert=True)
                return
        if callback.message is not None:
            await callback.message.answer(_("notify.topup.approved", balance=f"{r['balance']:,}"))
        await callback.answer()
        return

    # reject → collect a reason
    await state.set_state(WalletAdminStates.waiting_reason)
    await state.update_data(topup_id=topup_id, admin_tg=callback.from_user.id)
    if callback.message is not None:
        await callback.message.answer(_("notify.topup.ask_reason"))
    await callback.answer()


@router.message(WalletAdminStates.waiting_reason, Command("cancel"))
async def on_reject_cancel(message: Message, _: Callable[..., str], state: FSMContext) -> None:
    await state.clear()
    await message.answer(_("radm.cancelled"))


@router.message(WalletAdminStates.waiting_reason, F.text)
async def on_topup_reject_reason(
    message: Message, _: Callable[..., str], state: FSMContext,
    bot=None, is_admin: bool = False, lang: str = "fa",
) -> None:
    if not is_admin:
        await state.clear()
        return
    data = await state.get_data()
    if data.get("admin_tg") != message.from_user.id:
        return  # only the admin who started the reject may finish it
    topup_id = data.get("topup_id")
    reason = (message.text or "").strip()
    if not reason:
        await message.answer(_("notify.topup.ask_reason"))
        return
    async with SessionLocal() as session:
        try:
            await wallet_service.reject_topup(session, topup_id, admin_id=None, reason=reason, bot=bot)
            await session.commit()
        except WalletError:
            await state.clear()
            await message.answer(_("notify.topup.err"))
            return
    await state.clear()
    await message.answer(_("notify.topup.rejected"))

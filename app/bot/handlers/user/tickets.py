"""Phase 9 user support: /support + /tickets — create tickets, browse them,
reply, close, reopen. A user only ever sees/acts on their own tickets.

Message/attachment steps accept text OR a photo/document (caption = message).
This router is registered BEFORE user.orders so its state-filtered photo/document
handlers win over the stateless order-receipt handler.
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

from app.bot.handlers.user.orders import _download_telegram_file, _extract_file
from app.core.settings_service import SettingsService
from app.database import SessionLocal
from app.i18n import menu_texts
from app.services import ticket_service, user_service
from app.services.ticket_service import TicketAttachment, TicketError

log = logging.getLogger("bot.user.tickets")

router = Router(name="user.tickets")

CB_NEW = "tk_new"
CB_LIST = "tk_list"
CB_OPEN = "tko:"     # open ticket detail
CB_REPLY = "tkr:"    # start a reply
CB_CLOSE = "tkc:"    # close ticket
CB_REOPEN = "tkre:"  # reopen ticket

# Menu buttons that must never be swallowed by the subject/message/reply prompts.
_NAV_TEXTS: set[str] = set()
for _key in ("btn.products", "btn.account", "btn.support", "btn.rules", "btn.language",
             "btn.admin_panel", "btn.my_orders", "btn.my_licenses", "btn.my_services",
             "btn.wallet", "btn.tutorials"):
    _NAV_TEXTS |= menu_texts(_key)


class TicketStates(StatesGroup):
    waiting_subject = State()
    waiting_message = State()
    waiting_reply = State()


def _status_label(_: Callable[..., str], status: str) -> str:
    return _("ticket.status." + status)


def _support_menu_kb(_: Callable[..., str]) -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text=_("tickets.btn.new"), callback_data=CB_NEW)],
        [InlineKeyboardButton(text=_("tickets.btn.mine"), callback_data=CB_LIST)],
    ])


async def _show_support(message: Message, _: Callable[..., str]) -> None:
    async with SessionLocal() as session:
        if not await ticket_service.support_enabled(session):
            await message.answer(_("tickets.disabled"))
            return
        username = (await SettingsService(session).get_str("support_username", "")).strip()
    text = _("tickets.support_intro")
    if username:
        text += "\n" + _("tickets.support_username", username=username.lstrip("@"))
    await message.answer(text, reply_markup=_support_menu_kb(_))


@router.message(Command("support"))
@router.message(F.text.in_(menu_texts("btn.support")))
async def on_support(
    message: Message, _: Callable[..., str], state: FSMContext, lang: str = "fa"
) -> None:
    await state.clear()
    await _show_support(message, _)


# --- create ticket ----------------------------------------------------------
async def _start_new_ticket(message: Message, _: Callable[..., str], state: FSMContext) -> None:
    async with SessionLocal() as session:
        if not await ticket_service.support_enabled(session):
            await message.answer(_("tickets.disabled"))
            return
    await state.set_state(TicketStates.waiting_subject)
    await message.answer(_("tickets.ask_subject"))


@router.callback_query(F.data == CB_NEW)
async def on_new_cb(
    callback: CallbackQuery, _: Callable[..., str], state: FSMContext, lang: str = "fa"
) -> None:
    await callback.answer()
    if callback.message is not None:
        await _start_new_ticket(callback.message, _, state)


@router.message(TicketStates.waiting_subject, F.text, ~F.text.startswith("/"),
                ~F.text.in_(_NAV_TEXTS))
async def on_subject(
    message: Message, _: Callable[..., str], state: FSMContext, lang: str = "fa"
) -> None:
    subject = (message.text or "").strip()
    if not subject:
        await message.answer(_("tickets.ask_subject"))
        return
    await state.update_data(subject=subject[:200])
    await state.set_state(TicketStates.waiting_message)
    await message.answer(_("tickets.ask_message"))


async def _attachment_from_message(message: Message, bot: Bot, _: Callable[..., str]):
    """Extract + download an attachment from a photo/document message, or None."""
    extracted = _extract_file(message)
    if extracted is None:
        return None
    file_id, name, mime, size = extracted
    try:
        ticket_service.precheck_attachment(name, size, mime)
    except TicketError:
        await message.answer(_("tickets.attach_bad_type"))
        return "reject"
    try:
        content = await _download_telegram_file(bot, file_id)
    except Exception as exc:  # noqa: BLE001
        log.warning("ticket attachment download failed: %s", exc)
        await message.answer(_("tickets.attach_failed"))
        return "reject"
    return TicketAttachment(content=content, original_name=name, mime_type=mime, file_id=file_id)


@router.message(TicketStates.waiting_message, F.photo | F.document)
async def on_message_attachment(
    message: Message, bot: Bot, _: Callable[..., str], state: FSMContext, lang: str = "fa"
) -> None:
    att = await _attachment_from_message(message, bot, _)
    if att == "reject":
        return
    data = await state.get_data()
    body = (message.caption or "").strip()
    await _create_ticket(message, _, state, data.get("subject", ""), body, att)


@router.message(TicketStates.waiting_message, F.text, ~F.text.startswith("/"),
                ~F.text.in_(_NAV_TEXTS))
async def on_message_text(
    message: Message, _: Callable[..., str], state: FSMContext, lang: str = "fa"
) -> None:
    data = await state.get_data()
    await _create_ticket(message, _, state, data.get("subject", ""),
                         (message.text or "").strip(), None)


async def _create_ticket(message: Message, _: Callable[..., str], state: FSMContext,
                         subject: str, body: str, attachment) -> None:
    tg = message.from_user
    async with SessionLocal() as session:
        user, _c = await user_service.create_or_update_from_telegram(
            session, telegram_id=tg.id, username=tg.username,
            first_name=tg.first_name, last_name=tg.last_name)
        await session.commit()
        try:
            ticket = await ticket_service.create_ticket(
                session, user.id, subject, body, attachment=attachment)
            await session.commit()
        except TicketError as exc:
            await message.answer(_("tickets.error", error=str(exc)))
            return
        number = ticket.ticket_number
    await state.clear()
    await message.answer(_("tickets.created", number=number), parse_mode="HTML")


# --- list + open ------------------------------------------------------------
@router.message(Command("tickets"))
@router.message(F.text.in_(menu_texts("btn.my_tickets")))
async def on_my_tickets(
    message: Message, _: Callable[..., str], state: FSMContext, lang: str = "fa"
) -> None:
    await state.clear()
    await _send_ticket_list(message, _)


@router.callback_query(F.data == CB_LIST)
async def on_list_cb(
    callback: CallbackQuery, _: Callable[..., str], lang: str = "fa"
) -> None:
    await callback.answer()
    if callback.message is not None:
        await _send_ticket_list(callback.message, _, tg_id=callback.from_user.id)


async def _send_ticket_list(message: Message, _: Callable[..., str], *, tg_id: int | None = None):
    tg_id = tg_id or message.from_user.id
    async with SessionLocal() as session:
        user = await user_service.get_by_telegram_id(session, tg_id)
        tickets = await ticket_service.list_user_tickets(session, user.id) if user else []
    if not tickets:
        await message.answer(_("tickets.empty"))
        return
    lines = [_("tickets.list_title"), ""]
    buttons: list[list[InlineKeyboardButton]] = []
    for tk in tickets:
        lines.append(_("tickets.list_row", number=tk.ticket_number,
                       subject=tk.subject, status=_status_label(_, tk.status)))
        buttons.append([InlineKeyboardButton(
            text=f"{tk.ticket_number} · {tk.subject[:24]}", callback_data=f"{CB_OPEN}{tk.id}")])
    await message.answer("\n".join(lines), parse_mode="HTML",
                         reply_markup=InlineKeyboardMarkup(inline_keyboard=buttons))


def _ticket_detail_kb(_: Callable[..., str], tk) -> InlineKeyboardMarkup:
    rows: list[list[InlineKeyboardButton]] = []
    if tk.status != "closed":
        rows.append([
            InlineKeyboardButton(text=_("tickets.btn.reply"), callback_data=f"{CB_REPLY}{tk.id}"),
            InlineKeyboardButton(text=_("tickets.btn.close"), callback_data=f"{CB_CLOSE}{tk.id}"),
        ])
    else:
        rows.append([InlineKeyboardButton(
            text=_("tickets.btn.reopen"), callback_data=f"{CB_REOPEN}{tk.id}")])
    return InlineKeyboardMarkup(inline_keyboard=rows)


@router.callback_query(F.data.startswith(CB_OPEN))
async def on_open(
    callback: CallbackQuery, _: Callable[..., str], lang: str = "fa"
) -> None:
    ticket_id = int((callback.data or "0")[len(CB_OPEN):])
    text: str | None = None
    kb: InlineKeyboardMarkup | None = None
    async with SessionLocal() as session:
        user = await user_service.get_by_telegram_id(session, callback.from_user.id)
        tk = await ticket_service.get_ticket(session, ticket_id)
        if user is None or tk is None or tk.user_id != user.id:
            await callback.answer(_("tickets.not_found"), show_alert=True)
            return
        lines = [_("tickets.detail_title", number=tk.ticket_number),
                 _("tickets.detail_subject", subject=tk.subject),
                 _("tickets.detail_status", status=_status_label(_, tk.status)), ""]
        for m in tk.messages:
            who = _("ticket.sender." + m.sender_type)
            when = m.created_at.strftime("%Y-%m-%d %H:%M") if m.created_at else ""
            body = m.message or (_("tickets.attachment") if m.attachment_path else "")
            lines.append(f"<b>{who}</b> <i>{when}</i>\n{body}")
            if m.attachment_path:
                lines.append("📎 " + (m.attachment_original_name or _("tickets.attachment")))
            lines.append("")
        text = "\n".join(lines)
        kb = _ticket_detail_kb(_, tk)
    await callback.answer()
    if callback.message is not None and text:
        await callback.message.answer(text, parse_mode="HTML", reply_markup=kb)


# --- reply / close / reopen -------------------------------------------------
@router.callback_query(F.data.startswith(CB_REPLY))
async def on_reply_cb(
    callback: CallbackQuery, _: Callable[..., str], state: FSMContext, lang: str = "fa"
) -> None:
    ticket_id = int((callback.data or "0")[len(CB_REPLY):])
    await callback.answer()
    await state.set_state(TicketStates.waiting_reply)
    await state.update_data(ticket_id=ticket_id)
    if callback.message is not None:
        await callback.message.answer(_("tickets.ask_reply"))


@router.message(TicketStates.waiting_reply, F.photo | F.document)
async def on_reply_attachment(
    message: Message, bot: Bot, _: Callable[..., str], state: FSMContext, lang: str = "fa"
) -> None:
    att = await _attachment_from_message(message, bot, _)
    if att == "reject":
        return
    data = await state.get_data()
    await _submit_reply(message, _, state, data.get("ticket_id"),
                        (message.caption or "").strip(), att)


@router.message(TicketStates.waiting_reply, F.text, ~F.text.startswith("/"),
                ~F.text.in_(_NAV_TEXTS))
async def on_reply_text(
    message: Message, _: Callable[..., str], state: FSMContext, lang: str = "fa"
) -> None:
    data = await state.get_data()
    await _submit_reply(message, _, state, data.get("ticket_id"),
                        (message.text or "").strip(), None)


async def _submit_reply(message: Message, _: Callable[..., str], state: FSMContext,
                        ticket_id, body: str, attachment) -> None:
    if not ticket_id:
        await state.clear()
        return
    tg = message.from_user
    async with SessionLocal() as session:
        user = await user_service.get_by_telegram_id(session, tg.id)
        if user is None:
            await state.clear()
            return
        try:
            await ticket_service.add_user_reply(
                session, int(ticket_id), user.id, body, attachment=attachment)
            await session.commit()
        except TicketError as exc:
            await message.answer(_("tickets.error", error=str(exc)))
            return
    await state.clear()
    await message.answer(_("tickets.reply_sent"))


@router.callback_query(F.data.startswith(CB_CLOSE))
async def on_close(
    callback: CallbackQuery, _: Callable[..., str], lang: str = "fa"
) -> None:
    ticket_id = int((callback.data or "0")[len(CB_CLOSE):])
    async with SessionLocal() as session:
        user = await user_service.get_by_telegram_id(session, callback.from_user.id)
        if user is None:
            await callback.answer(_("tickets.not_found"), show_alert=True)
            return
        try:
            await ticket_service.close_ticket(
                session, ticket_id, actor_id=user.id, actor_type="user", user_id=user.id)
            await session.commit()
        except TicketError as exc:
            await callback.answer(str(exc), show_alert=True)
            return
    await callback.answer(_("tickets.closed"))


@router.callback_query(F.data.startswith(CB_REOPEN))
async def on_reopen(
    callback: CallbackQuery, _: Callable[..., str], lang: str = "fa"
) -> None:
    ticket_id = int((callback.data or "0")[len(CB_REOPEN):])
    async with SessionLocal() as session:
        user = await user_service.get_by_telegram_id(session, callback.from_user.id)
        if user is None:
            await callback.answer(_("tickets.not_found"), show_alert=True)
            return
        try:
            await ticket_service.reopen_ticket(session, ticket_id, user.id)
            await session.commit()
        except TicketError as exc:
            await callback.answer(str(exc), show_alert=True)
            return
    await callback.answer(_("tickets.reopened"))

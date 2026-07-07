"""Phase 9 Telegram admin support: /admin_tickets + /admin_ticket <number>.

A permitted admin lists open tickets, opens one, replies (text or attachment),
closes, assigns to self, and changes priority. `view_tickets` sees; `manage_tickets`
acts. Telegram↔admin linking doesn't exist yet, so actions are attributed to the
bootstrap super-admin row when one exists (see admin_service.resolve_admin_id).
"""
from __future__ import annotations

import logging
from collections.abc import Callable

from aiogram import Bot, F, Router
from aiogram.filters import Command, CommandObject
from aiogram.fsm.context import FSMContext
from aiogram.fsm.state import State, StatesGroup
from aiogram.types import (
    CallbackQuery,
    InlineKeyboardButton,
    InlineKeyboardMarkup,
    Message,
)

from app.bot.handlers.user.orders import _download_telegram_file, _extract_file
from app.core.permissions import Role, has_permission
from app.database import SessionLocal
from app.models.ticket import TICKET_PRIORITIES
from app.services import admin_service, ticket_service
from app.services.ticket_service import TicketAttachment, TicketError

log = logging.getLogger("bot.admin.tickets")

router = Router(name="admin.tickets")

CB = "tadm:"     # tadm:<action>:<ticket_id>
CB_PRIO = "tadmp:"  # tadmp:<ticket_id>:<priority>


class TicketAdminStates(StatesGroup):
    waiting_reply = State()


def _detail_kb(_: Callable[..., str], tk) -> InlineKeyboardMarkup:
    def b(action: str, key: str) -> InlineKeyboardButton:
        return InlineKeyboardButton(text=_(key), callback_data=f"{CB}{action}:{tk.id}")
    rows: list[list[InlineKeyboardButton]] = []
    if tk.status != "closed":
        rows.append([b("reply", "atickets.btn.reply"), b("close", "atickets.btn.close")])
    rows.append([b("assign", "atickets.btn.assign"), b("prio", "atickets.btn.priority")])
    return InlineKeyboardMarkup(inline_keyboard=rows)


def _render_ticket(_: Callable[..., str], tk) -> str:
    lines = [
        _("atickets.detail_title", number=tk.ticket_number),
        _("atickets.detail_subject", subject=tk.subject),
        _("atickets.detail_status", status=_("ticket.status." + tk.status)),
        _("atickets.detail_priority", priority=_("ticket.priority." + tk.priority)),
        "",
    ]
    for m in tk.messages:
        who = _("ticket.sender." + m.sender_type)
        when = m.created_at.strftime("%Y-%m-%d %H:%M") if m.created_at else ""
        body = m.message or (_("tickets.attachment") if m.attachment_path else "")
        lines.append(f"<b>{who}</b> <i>{when}</i>\n{body}")
        if m.attachment_path:
            lines.append("📎 " + (m.attachment_original_name or _("tickets.attachment")))
        lines.append("")
    return "\n".join(lines)


@router.message(Command("admin_tickets"))
async def on_admin_tickets(
    message: Message, _: Callable[..., str], state: FSMContext,
    role: Role | None = None, is_admin: bool = False, lang: str = "fa",
) -> None:
    await state.clear()
    if not is_admin or not has_permission(role, "view_tickets"):
        await message.answer(_("radm.not_authorized"))
        return
    async with SessionLocal() as session:
        tickets = await ticket_service.list_admin_tickets(session, status="open", limit=20)
    if not tickets:
        await message.answer(_("atickets.none"))
        return
    lines = [_("atickets.open_title"), ""]
    buttons: list[list[InlineKeyboardButton]] = []
    for tk in tickets:
        lines.append(_("atickets.row", number=tk.ticket_number, subject=tk.subject,
                       status=_("ticket.status." + tk.status),
                       priority=_("ticket.priority." + tk.priority)))
        buttons.append([InlineKeyboardButton(
            text=f"{tk.ticket_number} · {tk.subject[:22]}", callback_data=f"{CB}open:{tk.id}")])
    await message.answer("\n".join(lines), parse_mode="HTML",
                         reply_markup=InlineKeyboardMarkup(inline_keyboard=buttons))


@router.message(Command("admin_ticket"))
async def on_admin_ticket(
    message: Message, command: CommandObject, _: Callable[..., str], state: FSMContext,
    role: Role | None = None, is_admin: bool = False, lang: str = "fa",
) -> None:
    await state.clear()
    if not is_admin or not has_permission(role, "view_tickets"):
        await message.answer(_("radm.not_authorized"))
        return
    number = (command.args or "").strip()
    if not number:
        await message.answer(_("atickets.usage"))
        return
    async with SessionLocal() as session:
        tk = await ticket_service.get_ticket_by_number(session, number)
        if tk is None:
            await message.answer(_("atickets.not_found"))
            return
        text = _render_ticket(_, tk)
        kb = _detail_kb(_, tk) if has_permission(role, "manage_tickets") else None
    await message.answer(text, parse_mode="HTML", reply_markup=kb)


def _parse(data: str | None, prefix: str) -> tuple[str, int] | None:
    parts = (data or "").split(":")
    if len(parts) != 3 or (parts[0] + ":") != prefix:
        return None
    try:
        return parts[1], int(parts[2])
    except ValueError:
        return None


@router.callback_query(F.data.startswith(CB_PRIO))
async def on_priority_pick(
    callback: CallbackQuery, _: Callable[..., str],
    role: Role | None = None, is_admin: bool = False, lang: str = "fa",
) -> None:
    parts = (callback.data or "").split(":")
    if len(parts) != 3:
        await callback.answer()
        return
    ticket_id, priority = int(parts[1]), parts[2]
    if not is_admin or not has_permission(role, "manage_tickets"):
        await callback.answer(_("radm.not_authorized"), show_alert=True)
        return
    async with SessionLocal() as session:
        admin_id = await admin_service.resolve_admin_id(session, callback.from_user.id)
        try:
            await ticket_service.set_priority(session, ticket_id, priority, admin_id=admin_id)
            await session.commit()
        except TicketError as exc:
            await callback.answer(str(exc), show_alert=True)
            return
    await callback.answer(_("atickets.priority_set", priority=_("ticket.priority." + priority)))


@router.callback_query(F.data.startswith(CB))
async def on_ticket_action(
    callback: CallbackQuery, _: Callable[..., str], state: FSMContext,
    bot: Bot = None, role: Role | None = None, is_admin: bool = False, lang: str = "fa",
) -> None:
    parsed = _parse(callback.data, CB)
    if parsed is None:
        await callback.answer()
        return
    action, ticket_id = parsed
    if not is_admin:
        await callback.answer(_("radm.not_authorized"), show_alert=True)
        return
    # `open` needs view; everything else needs manage.
    need = "view_tickets" if action == "open" else "manage_tickets"
    if not has_permission(role, need):
        await callback.answer(_("radm.not_authorized"), show_alert=True)
        return

    if action == "open":
        async with SessionLocal() as session:
            tk = await ticket_service.get_ticket(session, ticket_id)
            if tk is None:
                await callback.answer(_("atickets.not_found"), show_alert=True)
                return
            text = _render_ticket(_, tk)
            kb = _detail_kb(_, tk) if has_permission(role, "manage_tickets") else None
        await callback.answer()
        if callback.message is not None:
            await callback.message.answer(text, parse_mode="HTML", reply_markup=kb)
        return

    if action == "close":
        async with SessionLocal() as session:
            admin_id = await admin_service.resolve_admin_id(session, callback.from_user.id)
            try:
                await ticket_service.close_ticket(
                    session, ticket_id, actor_id=admin_id, actor_type="admin")
                await session.commit()
            except TicketError as exc:
                await callback.answer(str(exc), show_alert=True)
                return
        await callback.answer(_("atickets.closed"))
        return

    if action == "assign":
        async with SessionLocal() as session:
            admin_id = await admin_service.resolve_admin_id(session, callback.from_user.id)
            if admin_id is None:
                await callback.answer(_("atickets.no_admin_row"), show_alert=True)
                return
            try:
                await ticket_service.assign_ticket(session, ticket_id, admin_id)
                await session.commit()
            except TicketError as exc:
                await callback.answer(str(exc), show_alert=True)
                return
        await callback.answer(_("atickets.assigned"))
        return

    if action == "prio":
        kb = InlineKeyboardMarkup(inline_keyboard=[[InlineKeyboardButton(
            text=_("ticket.priority." + p), callback_data=f"{CB_PRIO}{ticket_id}:{p}")]
            for p in TICKET_PRIORITIES])
        await callback.answer()
        if callback.message is not None:
            await callback.message.answer(_("atickets.pick_priority"), reply_markup=kb)
        return

    if action == "reply":
        await state.set_state(TicketAdminStates.waiting_reply)
        await state.update_data(ticket_id=ticket_id, admin_tg=callback.from_user.id)
        await callback.answer()
        if callback.message is not None:
            await callback.message.answer(_("atickets.ask_reply"))
        return


@router.message(TicketAdminStates.waiting_reply, Command("cancel"))
async def on_reply_cancel(message: Message, _: Callable[..., str], state: FSMContext) -> None:
    await state.clear()
    await message.answer(_("radm.cancelled"))


@router.message(TicketAdminStates.waiting_reply, F.text | F.photo | F.document)
async def on_admin_reply(
    message: Message, _: Callable[..., str], state: FSMContext,
    bot: Bot = None, is_admin: bool = False, lang: str = "fa",
) -> None:
    if not is_admin:
        await state.clear()
        return
    data = await state.get_data()
    if data.get("admin_tg") != message.from_user.id:
        return
    ticket_id = data.get("ticket_id")
    body = (message.text or message.caption or "").strip()

    attachment = None
    extracted = _extract_file(message)
    if extracted is not None:
        file_id, name, mime, size = extracted
        try:
            ticket_service.precheck_attachment(name, size, mime)
            content = await _download_telegram_file(bot, file_id)
            attachment = TicketAttachment(content=content, original_name=name,
                                          mime_type=mime, file_id=file_id)
        except Exception:  # noqa: BLE001 - fall back to a text-only reply
            attachment = None

    async with SessionLocal() as session:
        admin_id = await admin_service.resolve_admin_id(session, message.from_user.id)
        try:
            ticket = await ticket_service.add_admin_reply(
                session, int(ticket_id), admin_id, body, attachment=attachment)
            await session.commit()
        except TicketError as exc:
            await state.clear()
            await message.answer(_("tickets.error", error=str(exc)))
            return
        user = ticket.user
        number = ticket.ticket_number
    await ticket_service.notify_user(bot, user, "ticket.notify.admin_reply", number=number)
    await state.clear()
    await message.answer(_("atickets.reply_sent"))

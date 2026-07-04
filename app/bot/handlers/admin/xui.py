"""Telegram admin 3X-UI server management (RBAC-gated FSM, bilingual).

Add flow: pick version -> name -> base URL -> web path -> username -> password
-> create (credentials encrypted at rest by app/services/xui_service). Managing
servers requires the manage_xui permission. Each server row exposes Test
connection and Sync inbounds, both of which reach the panel through the service
layer only. No panel secret is ever echoed back to the chat.
"""
from __future__ import annotations

import logging
from collections.abc import Callable

from aiogram import F, Router
from aiogram.fsm.context import FSMContext
from aiogram.filters import Command
from aiogram.types import CallbackQuery, InlineKeyboardButton, InlineKeyboardMarkup, Message

from app.bot.states.xui import ServerAddForm
from app.core.permissions import Role, has_permission
from app.database import SessionLocal
from app.i18n import t, texts_for
from app.models.xui_server import XuiServer
from app.services import xui_service
from app.xui.exceptions import XuiError
from app.xui.registry import SUPPORTED_VERSIONS

log = logging.getLogger("bot.xui")

router = Router(name="admin.xui")

CB_LIST = "srv:list"
CB_ADD = "srv:add"
CB_VERSION = "srvver:"
CB_TEST = "srv:test:"
CB_SYNC = "srv:sync:"


def _status_text(status: str, lang: str) -> str:
    key = {
        "online": "xui.status.online",
        "offline": "xui.status.offline",
        "auth_error": "xui.status.auth_error",
    }.get(status, "xui.status.unknown")
    return t(key, lang)


def _server_line(server: XuiServer, lang: str) -> str:
    return " · ".join(
        [
            f"<b>{server.name}</b>",
            server.panel_version,
            _status_text(server.status, lang),
        ]
    )


async def _admin_overview(lang: str) -> tuple[str, InlineKeyboardMarkup]:
    async with SessionLocal() as session:
        servers = await xui_service.list_servers(session)

    lines = [t("servers.admin.header", lang), ""]
    buttons: list[list[InlineKeyboardButton]] = []
    if not servers:
        lines.append(t("servers.admin.empty", lang))
    for s in servers:
        lines.append(f"• {_server_line(s, lang)}")
        buttons.append(
            [
                InlineKeyboardButton(
                    text=f"🔌 {s.name}", callback_data=f"{CB_TEST}{s.id}"
                ),
                InlineKeyboardButton(
                    text=t("btn.srv.sync", lang), callback_data=f"{CB_SYNC}{s.id}"
                ),
            ]
        )
    buttons.append(
        [InlineKeyboardButton(text=t("servers.admin.add", lang), callback_data=CB_ADD)]
    )
    return "\n".join(lines), InlineKeyboardMarkup(inline_keyboard=buttons)


@router.message(F.text.in_(texts_for("btn.admin.servers")))
async def on_servers_menu(
    message: Message, _: Callable[..., str], lang: str = "fa", role: Role | None = None
) -> None:
    if not has_permission(role, "manage_xui"):
        await message.answer(_("xui.not_authorized"))
        return
    text, keyboard = await _admin_overview(lang)
    await message.answer(text, reply_markup=keyboard, parse_mode="HTML")


@router.callback_query(F.data == CB_LIST)
async def on_back_to_list(
    callback: CallbackQuery, _: Callable[..., str], lang: str = "fa", role: Role | None = None
) -> None:
    if not has_permission(role, "manage_xui"):
        await callback.answer(_("xui.not_authorized"), show_alert=True)
        return
    text, keyboard = await _admin_overview(lang)
    if isinstance(callback.message, Message):
        await callback.message.edit_text(text, reply_markup=keyboard, parse_mode="HTML")
    await callback.answer()


@router.callback_query(F.data == CB_ADD)
async def on_add(
    callback: CallbackQuery,
    state: FSMContext,
    _: Callable[..., str],
    lang: str = "fa",
    role: Role | None = None,
) -> None:
    if not has_permission(role, "manage_xui"):
        await callback.answer(_("xui.not_authorized"), show_alert=True)
        return
    keyboard = InlineKeyboardMarkup(
        inline_keyboard=[
            [
                InlineKeyboardButton(
                    text=version, callback_data=f"{CB_VERSION}{version}"
                )
                for version in SUPPORTED_VERSIONS
            ]
        ]
    )
    await state.set_state(ServerAddForm.picking_version)
    if isinstance(callback.message, Message):
        await callback.message.answer(_("servers.pick_version"), reply_markup=keyboard)
    await callback.answer()


@router.callback_query(ServerAddForm.picking_version, F.data.startswith(CB_VERSION))
async def on_version_chosen(
    callback: CallbackQuery,
    state: FSMContext,
    _: Callable[..., str],
    role: Role | None = None,
) -> None:
    if not has_permission(role, "manage_xui"):
        await callback.answer(_("xui.not_authorized"), show_alert=True)
        return
    version = (callback.data or "")[len(CB_VERSION):]
    if version not in SUPPORTED_VERSIONS:
        await callback.answer(_("servers.unknown"), show_alert=True)
        return
    await state.update_data(panel_version=version)
    await state.set_state(ServerAddForm.entering_name)
    if isinstance(callback.message, Message):
        await callback.message.answer(_("servers.ask_name"))
    await callback.answer()


@router.message(ServerAddForm.picking_version, Command("cancel"))
@router.message(ServerAddForm.entering_name, Command("cancel"))
@router.message(ServerAddForm.entering_base_url, Command("cancel"))
@router.message(ServerAddForm.entering_path, Command("cancel"))
@router.message(ServerAddForm.entering_username, Command("cancel"))
@router.message(ServerAddForm.entering_password, Command("cancel"))
async def on_cancel(message: Message, state: FSMContext, _: Callable[..., str]) -> None:
    await state.clear()
    await message.answer(_("servers.cancelled"))


@router.message(ServerAddForm.entering_name, F.text)
async def on_name(message: Message, state: FSMContext, _: Callable[..., str]) -> None:
    name = (message.text or "").strip()
    if not name:
        await message.answer(_("servers.ask_name"))
        return
    await state.update_data(name=name)
    await state.set_state(ServerAddForm.entering_base_url)
    await message.answer(_("servers.ask_base_url"))


@router.message(ServerAddForm.entering_base_url, F.text)
async def on_base_url(message: Message, state: FSMContext, _: Callable[..., str]) -> None:
    base_url = (message.text or "").strip()
    if not base_url:
        await message.answer(_("servers.ask_base_url"))
        return
    await state.update_data(base_url=base_url)
    await state.set_state(ServerAddForm.entering_path)
    await message.answer(_("servers.ask_path"))


@router.message(ServerAddForm.entering_path, F.text)
async def on_path(message: Message, state: FSMContext, _: Callable[..., str]) -> None:
    raw = (message.text or "").strip()
    web_base_path = None if raw in ("", "-") else raw
    await state.update_data(web_base_path=web_base_path)
    await state.set_state(ServerAddForm.entering_username)
    await message.answer(_("servers.ask_username"))


@router.message(ServerAddForm.entering_username, F.text)
async def on_username(message: Message, state: FSMContext, _: Callable[..., str]) -> None:
    username = (message.text or "").strip()
    if not username:
        await message.answer(_("servers.ask_username"))
        return
    await state.update_data(username=username)
    await state.set_state(ServerAddForm.entering_password)
    await message.answer(_("servers.ask_password"))


@router.message(ServerAddForm.entering_password, F.text)
async def on_password(
    message: Message, state: FSMContext, _: Callable[..., str], role: Role | None = None
) -> None:
    if not has_permission(role, "manage_xui"):
        await state.clear()
        await message.answer(_("xui.not_authorized"))
        return
    password = message.text or ""
    data = await state.get_data()
    await state.clear()
    tg_user = message.from_user
    try:
        async with SessionLocal() as session:
            server = await xui_service.add_server(
                session,
                name=str(data.get("name", "")),
                base_url=str(data.get("base_url", "")),
                username=str(data.get("username", "")),
                password=password,
                web_base_path=data.get("web_base_path"),
                panel_version=str(data.get("panel_version", "2.9.4")),
                actor_type="admin",
                actor_id=tg_user.id if tg_user else None,
            )
    except (ValueError, TypeError) as exc:
        await message.answer(_("servers.unknown") + f"\n{exc}")
        return
    # Best-effort delete of the message that carried the plaintext password.
    try:
        await message.delete()
    except Exception:  # noqa: BLE001 - deletion is best-effort only
        pass
    await message.answer(_("servers.created", name=server.name))


@router.callback_query(F.data.startswith(CB_TEST))
async def on_test(
    callback: CallbackQuery, _: Callable[..., str], lang: str = "fa", role: Role | None = None
) -> None:
    if not has_permission(role, "manage_xui"):
        await callback.answer(_("xui.not_authorized"), show_alert=True)
        return
    server_id = int((callback.data or "0")[len(CB_TEST):])
    async with SessionLocal() as session:
        server = await xui_service.get_server(session, server_id)
        if server is None:
            await callback.answer(_("servers.unknown"), show_alert=True)
            return
        result = await xui_service.test_connection(session, server)
    if result.get("ok"):
        await callback.answer(t("xui.test.ok", lang), show_alert=True)
    else:
        await callback.answer(
            t("xui.test.fail", lang, message=result.get("message", "")), show_alert=True
        )
    text, keyboard = await _admin_overview(lang)
    if isinstance(callback.message, Message):
        await callback.message.edit_text(text, reply_markup=keyboard, parse_mode="HTML")


@router.callback_query(F.data.startswith(CB_SYNC))
async def on_sync(
    callback: CallbackQuery, _: Callable[..., str], lang: str = "fa", role: Role | None = None
) -> None:
    if not has_permission(role, "manage_xui"):
        await callback.answer(_("xui.not_authorized"), show_alert=True)
        return
    server_id = int((callback.data or "0")[len(CB_SYNC):])
    async with SessionLocal() as session:
        server = await xui_service.get_server(session, server_id)
        if server is None:
            await callback.answer(_("servers.unknown"), show_alert=True)
            return
        try:
            count = await xui_service.sync_inbounds(session, server)
        except XuiError as exc:
            await callback.answer(
                t("xui.test.fail", lang, message=str(exc)), show_alert=True
            )
            return
    await callback.answer(t("xui.sync.ok", lang, count=count), show_alert=True)
    text, keyboard = await _admin_overview(lang)
    if isinstance(callback.message, Message):
        await callback.message.edit_text(text, reply_markup=keyboard, parse_mode="HTML")

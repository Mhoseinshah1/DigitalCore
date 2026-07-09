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
from app.services import xui_inbound_sync_service, xui_service
from app.xui.registry import SUPPORTED_VERSIONS

log = logging.getLogger("bot.xui")

router = Router(name="admin.xui")

CB_LIST = "srv:list"
CB_ADD = "srv:add"
CB_VERSION = "srvver:"
CB_TEST = "srv:test:"
CB_SYNC = "srv:sync:"
CB_SYNC_ALL = "srv:syncall"


def _status_text(status: str, lang: str) -> str:
    key = {
        "online": "xui.status.online",
        "offline": "xui.status.offline",
        "auth_error": "xui.status.auth_error",
        "active": "xui.status.active",
        "error": "xui.status.error",
        "inactive": "xui.status.inactive",
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
    if servers:
        buttons.append(
            [InlineKeyboardButton(text=t("xui.sync.all_btn", lang), callback_data=CB_SYNC_ALL)]
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


@router.callback_query(F.data == CB_SYNC_ALL)
async def on_sync_all(
    callback: CallbackQuery, _: Callable[..., str], lang: str = "fa", role: Role | None = None
) -> None:
    if not has_permission(role, "manage_xui"):
        await callback.answer(_("xui.not_authorized"), show_alert=True)
        return
    await callback.answer()
    async with SessionLocal() as session:
        results = await xui_inbound_sync_service.sync_all_active_servers(session)
    if isinstance(callback.message, Message):
        await callback.message.answer(_sync_report(results, lang), parse_mode="HTML")
        text, keyboard = await _admin_overview(lang)
        await callback.message.answer(text, reply_markup=keyboard, parse_mode="HTML")


@router.callback_query(F.data.startswith(CB_SYNC))
async def on_sync(
    callback: CallbackQuery, _: Callable[..., str], lang: str = "fa", role: Role | None = None
) -> None:
    if not has_permission(role, "manage_xui"):
        await callback.answer(_("xui.not_authorized"), show_alert=True)
        return
    server_id = int((callback.data or "0")[len(CB_SYNC):])
    async with SessionLocal() as session:
        result = await xui_inbound_sync_service.sync_server_inbounds(session, server_id)
    if not result.success:
        await callback.answer(
            t("xui.test.fail", lang, message=result.error_message or ""), show_alert=True
        )
        return
    await callback.answer(
        t("xui.sync.detail", lang, total=result.total_remote_count,
          created=result.created_count, updated=result.updated_count,
          disabled=result.disabled_count),
        show_alert=True,
    )
    text, keyboard = await _admin_overview(lang)
    if isinstance(callback.message, Message):
        await callback.message.edit_text(text, reply_markup=keyboard, parse_mode="HTML")


def _sync_report(results: list, lang: str) -> str:
    """A safe, per-server diagnostic of a sync-all run (no secrets)."""
    from app.bot.utils.message_format import esc

    lines = [t("xui.sync.report_title", lang)]
    if not results:
        lines.append(t("xui.sync.no_servers", lang))
        return "\n".join(lines)
    for r in results:
        if r.success:
            lines.append(t(
                "xui.sync.report_ok", lang, name=esc(r.server_name),
                total=r.total_remote_count, created=r.created_count,
                updated=r.updated_count, disabled=r.disabled_count))
        else:
            lines.append(t(
                "xui.sync.report_fail", lang, name=esc(r.server_name),
                message=esc(r.error_message or "")))
    return "\n".join(lines)


@router.message(Command("xui_sync"))
async def on_xui_sync_command(
    message: Message, _: Callable[..., str], lang: str = "fa", role: Role | None = None
) -> None:
    """Diagnostic: sync every active 3X-UI server and report the outcome."""
    if not has_permission(role, "manage_xui"):
        await message.answer(_("xui.not_authorized"))
        return
    async with SessionLocal() as session:
        results = await xui_inbound_sync_service.sync_all_active_servers(session)
    await message.answer(_sync_report(results, lang), parse_mode="HTML")

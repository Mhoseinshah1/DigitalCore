"""Telegram admin settings editor (RBAC-gated FSM).

Flow: ⚙️ Settings (or /settings) lists all editable settings grouped by
category as inline buttons. Picking a boolean toggles it immediately; picking
anything else asks for the new value, validates it via SettingsService.set()
(which encrypts secrets and audit-logs the change), and confirms.

Handlers stay thin — all persistence/validation lives in the settings service.
"""
from __future__ import annotations

import logging

from aiogram import F, Router
from aiogram.filters import Command
from aiogram.fsm.context import FSMContext
from aiogram.types import CallbackQuery, InlineKeyboardButton, InlineKeyboardMarkup, Message

from app.bot.keyboards.admin import BTN_SETTINGS
from app.bot.states.settings import SettingsForm
from app.core.defaults import CATEGORIES, DEFAULTS, DEFAULTS_BY_KEY
from app.core.permissions import Role, has_permission
from app.core.settings_service import SECRET_MASK, SettingsService, coerce_out
from app.database import SessionLocal

log = logging.getLogger("bot.settings")

router = Router(name="admin.settings")

NOT_AUTHORIZED = "⛔️ You are not authorized to manage settings."
CB_PREFIX = "setting:"
MAX_PREVIEW = 24


def _preview(value: object, *, is_secret: bool, has_value: bool) -> str:
    if is_secret:
        return "•••" if has_value else "unset"
    text = str(value)
    if isinstance(value, bool):
        return "on" if value else "off"
    if not text:
        return "—"
    return text if len(text) <= MAX_PREVIEW else text[: MAX_PREVIEW - 1] + "…"


async def _settings_overview() -> tuple[str, InlineKeyboardMarkup]:
    """The grouped settings list + one inline button per setting."""
    async with SessionLocal() as session:
        svc = SettingsService(session)
        rows = {r.key: r for r in await svc.all_rows()}

        lines: list[str] = ["⚙️ <b>Settings</b> — pick one to edit:\n"]
        buttons: list[list[InlineKeyboardButton]] = []
        pending_row: list[InlineKeyboardButton] = []

        for cat, meta in sorted(CATEGORIES.items(), key=lambda kv: kv[1]["order"]):
            defs = [d for d in DEFAULTS if d.category == cat]
            if not defs:
                continue
            lines.append(f"\n{meta['icon']} <b>{meta['title']}</b>")
            for d in defs:
                row = rows.get(d.key)
                if d.is_secret:
                    value_text = _preview("", is_secret=True, has_value=bool(row and row.value))
                else:
                    current = coerce_out(d.value_type, row.value) if row else coerce_out(
                        d.value_type, d.default
                    )
                    value_text = _preview(current, is_secret=False, has_value=True)
                lines.append(f"  • {d.label}: <code>{value_text}</code>")
                pending_row.append(
                    InlineKeyboardButton(text=d.label, callback_data=f"{CB_PREFIX}{d.key}")
                )
                if len(pending_row) == 2:
                    buttons.append(pending_row)
                    pending_row = []
        if pending_row:
            buttons.append(pending_row)

    return "\n".join(lines), InlineKeyboardMarkup(inline_keyboard=buttons)


@router.message(Command("settings"))
@router.message(F.text == BTN_SETTINGS)
async def on_settings_menu(message: Message, role: Role | None = None) -> None:
    if not has_permission(role, "manage_settings"):
        await message.answer(NOT_AUTHORIZED)
        return
    text, keyboard = await _settings_overview()
    await message.answer(text, reply_markup=keyboard, parse_mode="HTML")


@router.callback_query(F.data.startswith(CB_PREFIX))
async def on_setting_chosen(
    callback: CallbackQuery, state: FSMContext, role: Role | None = None
) -> None:
    if not has_permission(role, "manage_settings"):
        await callback.answer(NOT_AUTHORIZED, show_alert=True)
        return
    key = (callback.data or "")[len(CB_PREFIX):]
    d = DEFAULTS_BY_KEY.get(key)
    if d is None:
        await callback.answer("Unknown setting.", show_alert=True)
        return

    tg_user = callback.from_user

    # Booleans toggle immediately.
    if d.value_type == "bool":
        async with SessionLocal() as session:
            svc = SettingsService(session)
            current = await svc.get_bool(key, False)
            await svc.set(key, not current, actor_type="admin", actor_id=tg_user.id)
        await callback.answer(f"{d.label}: {'off' if current else 'on'}")
        if isinstance(callback.message, Message):
            text, keyboard = await _settings_overview()
            await callback.message.edit_text(text, reply_markup=keyboard, parse_mode="HTML")
        return

    # Everything else: ask for the new value.
    async with SessionLocal() as session:
        svc = SettingsService(session)
        if d.is_secret:
            row = await svc.get_raw(key)
            current_text = "•••" if (row and row.value) else "unset"
        else:
            current_text = str(await svc.get(key, d.default or "—")) or "—"

    await state.set_state(SettingsForm.entering_value)
    await state.update_data(key=key)
    hint = "an integer" if d.value_type == "int" else "the new value"
    if isinstance(callback.message, Message):
        await callback.message.answer(
            f"✏️ <b>{d.label}</b>\n{d.description}\n\n"
            f"Current: <code>{current_text}</code>\n"
            f"Send {hint}, or /cancel to abort.",
            parse_mode="HTML",
        )
    await callback.answer()


@router.message(SettingsForm.entering_value, Command("cancel"))
async def on_cancel(message: Message, state: FSMContext) -> None:
    await state.clear()
    await message.answer("❌ Cancelled — nothing was changed.")


@router.message(SettingsForm.entering_value, F.text)
async def on_new_value(
    message: Message, state: FSMContext, role: Role | None = None
) -> None:
    if not has_permission(role, "manage_settings"):
        await state.clear()
        await message.answer(NOT_AUTHORIZED)
        return

    data = await state.get_data()
    key = str(data.get("key", ""))
    d = DEFAULTS_BY_KEY.get(key)
    if d is None:
        await state.clear()
        await message.answer("Something went wrong — please reopen ⚙️ Settings.")
        return

    tg_user = message.from_user
    value = (message.text or "").strip()
    try:
        async with SessionLocal() as session:
            await SettingsService(session).set(
                key, value, actor_type="admin", actor_id=tg_user.id if tg_user else None
            )
    except ValueError as exc:
        await message.answer(f"⚠️ Invalid value: {exc}\nTry again, or /cancel.")
        return

    await state.clear()
    shown = SECRET_MASK if d.is_secret else (value or "—")
    await message.answer(f"✅ {d.label} updated to: <code>{shown}</code>", parse_mode="HTML")

"""Telegram admin settings editor (RBAC-gated FSM, bilingual).

Flow: ⚙️ Settings (or /settings) lists all editable settings grouped by
category as inline buttons. Picking a boolean toggles it immediately; picking
anything else asks for the new value, validates it via SettingsService.set()
(which encrypts secrets and audit-logs the change), and confirms.

Handlers stay thin — all persistence/validation lives in the settings service;
all user-facing text goes through the i18n layer.
"""
from __future__ import annotations

import logging
from collections.abc import Callable

from aiogram import F, Router
from aiogram.filters import Command
from aiogram.fsm.context import FSMContext
from aiogram.types import CallbackQuery, InlineKeyboardButton, InlineKeyboardMarkup, Message

from app.bot.states.settings import SettingsForm
from app.core.defaults import (
    CATEGORIES,
    DEFAULTS,
    DEFAULTS_BY_KEY,
    category_title_for,
    description_for,
    label_for,
)
from app.core.permissions import Role, has_permission
from app.core.settings_service import SECRET_MASK, SettingsService, coerce_out
from app.database import SessionLocal
from app.i18n import t, texts_for

log = logging.getLogger("bot.settings")

router = Router(name="admin.settings")

CB_PREFIX = "setting:"
MAX_PREVIEW = 24


def _preview(value: object, lang: str, *, is_secret: bool, has_value: bool) -> str:
    if is_secret:
        return t("value.secret_set", lang) if has_value else t("value.unset", lang)
    if isinstance(value, bool):
        return t("value.on", lang) if value else t("value.off", lang)
    text = str(value)
    if not text:
        return t("value.empty", lang)
    return text if len(text) <= MAX_PREVIEW else text[: MAX_PREVIEW - 1] + "…"


async def _settings_overview(lang: str) -> tuple[str, InlineKeyboardMarkup]:
    """The grouped settings list + one inline button per setting."""
    async with SessionLocal() as session:
        svc = SettingsService(session)
        rows = {r.key: r for r in await svc.all_rows()}

        lines: list[str] = [t("settings.header", lang), ""]
        buttons: list[list[InlineKeyboardButton]] = []
        pending_row: list[InlineKeyboardButton] = []

        for cat, meta in sorted(CATEGORIES.items(), key=lambda kv: kv[1]["order"]):
            defs = [d for d in DEFAULTS if d.category == cat]
            if not defs:
                continue
            lines.append(f"\n{meta['icon']} <b>{category_title_for(cat, lang)}</b>")
            for d in defs:
                row = rows.get(d.key)
                if d.is_secret:
                    value_text = _preview(
                        "", lang, is_secret=True, has_value=bool(row and row.value)
                    )
                else:
                    current = coerce_out(d.value_type, row.value) if row else coerce_out(
                        d.value_type, d.default
                    )
                    value_text = _preview(current, lang, is_secret=False, has_value=True)
                label = label_for(d, lang)
                lines.append(f"  • {label}: <code>{value_text}</code>")
                pending_row.append(
                    InlineKeyboardButton(text=label, callback_data=f"{CB_PREFIX}{d.key}")
                )
                if len(pending_row) == 2:
                    buttons.append(pending_row)
                    pending_row = []
        if pending_row:
            buttons.append(pending_row)

    return "\n".join(lines), InlineKeyboardMarkup(inline_keyboard=buttons)


@router.message(Command("settings"))
@router.message(F.text.in_(texts_for("btn.admin.settings")))
async def on_settings_menu(
    message: Message, _: Callable[..., str], lang: str = "fa", role: Role | None = None
) -> None:
    if not has_permission(role, "manage_settings"):
        await message.answer(_("settings.not_authorized"))
        return
    text, keyboard = await _settings_overview(lang)
    await message.answer(text, reply_markup=keyboard, parse_mode="HTML")


@router.callback_query(F.data.startswith(CB_PREFIX))
async def on_setting_chosen(
    callback: CallbackQuery,
    state: FSMContext,
    _: Callable[..., str],
    lang: str = "fa",
    role: Role | None = None,
) -> None:
    if not has_permission(role, "manage_settings"):
        await callback.answer(_("settings.not_authorized"), show_alert=True)
        return
    key = (callback.data or "")[len(CB_PREFIX):]
    d = DEFAULTS_BY_KEY.get(key)
    if d is None:
        await callback.answer(_("settings.unknown"), show_alert=True)
        return

    tg_user = callback.from_user
    label = label_for(d, lang)

    # Booleans toggle immediately.
    if d.value_type == "bool":
        async with SessionLocal() as session:
            svc = SettingsService(session)
            current = await svc.get_bool(key, False)
            await svc.set(key, not current, actor_type="admin", actor_id=tg_user.id)
        state_text = t("value.off", lang) if current else t("value.on", lang)
        await callback.answer(_("settings.toggled", label=label, state=state_text))
        if isinstance(callback.message, Message):
            text, keyboard = await _settings_overview(lang)
            await callback.message.edit_text(text, reply_markup=keyboard, parse_mode="HTML")
        return

    # Everything else: ask for the new value.
    async with SessionLocal() as session:
        svc = SettingsService(session)
        if d.is_secret:
            row = await svc.get_raw(key)
            current_text = (
                t("value.secret_set", lang) if (row and row.value) else t("value.unset", lang)
            )
        else:
            current_text = str(await svc.get(key, d.default or "")) or t("value.empty", lang)

    await state.set_state(SettingsForm.entering_value)
    await state.update_data(key=key)
    if isinstance(callback.message, Message):
        await callback.message.answer(
            _(
                "settings.prompt",
                label=label,
                description=description_for(d, lang),
                current=current_text,
            ),
            parse_mode="HTML",
        )
    await callback.answer()


@router.message(SettingsForm.entering_value, Command("cancel"))
async def on_cancel(message: Message, state: FSMContext, _: Callable[..., str]) -> None:
    await state.clear()
    await message.answer(_("settings.cancelled"))


@router.message(SettingsForm.entering_value, F.text)
async def on_new_value(
    message: Message,
    state: FSMContext,
    _: Callable[..., str],
    lang: str = "fa",
    role: Role | None = None,
) -> None:
    if not has_permission(role, "manage_settings"):
        await state.clear()
        await message.answer(_("settings.not_authorized"))
        return

    data = await state.get_data()
    key = str(data.get("key", ""))
    d = DEFAULTS_BY_KEY.get(key)
    if d is None:
        await state.clear()
        await message.answer(_("settings.session_lost"))
        return

    tg_user = message.from_user
    value = (message.text or "").strip()
    try:
        async with SessionLocal() as session:
            await SettingsService(session).set(
                key, value, actor_type="admin", actor_id=tg_user.id if tg_user else None
            )
    except ValueError as exc:
        await message.answer(_("settings.invalid", error=exc))
        return

    await state.clear()
    shown = SECRET_MASK if d.is_secret else (value or t("value.empty", lang))
    await message.answer(
        _("settings.updated", label=label_for(d, lang), value=shown), parse_mode="HTML"
    )

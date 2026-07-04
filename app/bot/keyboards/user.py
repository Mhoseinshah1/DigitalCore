"""User main menu (built per-language)."""
from __future__ import annotations

from aiogram.types import KeyboardButton, ReplyKeyboardMarkup

from app.i18n import t


def user_main_menu(lang: str, *, is_admin: bool = False) -> ReplyKeyboardMarkup:
    rows: list[list[KeyboardButton]] = [
        [KeyboardButton(text=t("btn.products", lang)), KeyboardButton(text=t("btn.account", lang))],
        [KeyboardButton(text=t("btn.support", lang)), KeyboardButton(text=t("btn.rules", lang))],
        [KeyboardButton(text=t("btn.language", lang))],
    ]
    if is_admin:
        rows.append([KeyboardButton(text=t("btn.admin_panel", lang))])
    return ReplyKeyboardMarkup(keyboard=rows, resize_keyboard=True)

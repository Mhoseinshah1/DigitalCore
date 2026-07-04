"""User main menu."""
from __future__ import annotations

from aiogram.types import KeyboardButton, ReplyKeyboardMarkup

BTN_PRODUCTS = "🛍 Products"
BTN_ACCOUNT = "👤 My account"
BTN_SUPPORT = "💬 Support"
BTN_RULES = "ℹ️ Rules"
BTN_ADMIN_PANEL = "🛠 Admin panel"


def user_main_menu(*, is_admin: bool = False) -> ReplyKeyboardMarkup:
    rows: list[list[KeyboardButton]] = [
        [KeyboardButton(text=BTN_PRODUCTS), KeyboardButton(text=BTN_ACCOUNT)],
        [KeyboardButton(text=BTN_SUPPORT), KeyboardButton(text=BTN_RULES)],
    ]
    if is_admin:
        rows.append([KeyboardButton(text=BTN_ADMIN_PANEL)])
    return ReplyKeyboardMarkup(keyboard=rows, resize_keyboard=True)

"""Admin main menu."""
from __future__ import annotations

from aiogram.types import KeyboardButton, ReplyKeyboardMarkup

BTN_DASHBOARD = "📊 Dashboard"
BTN_USERS = "👥 Users"
BTN_BROADCAST = "📢 Broadcast"
BTN_SETTINGS = "⚙️ Settings"
BTN_BACK_TO_USER = "⬅️ User menu"


def admin_main_menu() -> ReplyKeyboardMarkup:
    return ReplyKeyboardMarkup(
        keyboard=[
            [KeyboardButton(text=BTN_DASHBOARD), KeyboardButton(text=BTN_USERS)],
            [KeyboardButton(text=BTN_BROADCAST), KeyboardButton(text=BTN_SETTINGS)],
            [KeyboardButton(text=BTN_BACK_TO_USER)],
        ],
        resize_keyboard=True,
    )

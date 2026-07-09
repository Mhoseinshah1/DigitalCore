"""Admin main menu (built per-language)."""
from __future__ import annotations

from aiogram.types import KeyboardButton, ReplyKeyboardMarkup

from app.i18n import t


def admin_main_menu(lang: str) -> ReplyKeyboardMarkup:
    return ReplyKeyboardMarkup(
        keyboard=[
            [
                KeyboardButton(text=t("btn.admin.stats", lang)),
                KeyboardButton(text=t("btn.admin.pending", lang)),
            ],
            [
                KeyboardButton(text=t("btn.admin.users", lang)),
                KeyboardButton(text=t("btn.admin.products", lang)),
            ],
            [
                KeyboardButton(text=t("btn.admin.servers", lang)),
                KeyboardButton(text=t("btn.admin.settings", lang)),
            ],
            [
                KeyboardButton(text=t("btn.admin.financial", lang)),
                KeyboardButton(text=t("btn.admin.back", lang)),
            ],
        ],
        resize_keyboard=True,
    )

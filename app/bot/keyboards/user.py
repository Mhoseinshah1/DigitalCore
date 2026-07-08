"""User main menu (built per-language).

The license section button label is configurable via the ``license_section_title``
setting (e.g. an operator selling Apple IDs can rename it to «اپل آیدی‌های من»).
``قوانین`` is intentionally NOT in the menu — the rules are shown on /start.
"""
from __future__ import annotations

from aiogram.types import KeyboardButton, ReplyKeyboardMarkup

from app.core.settings_service import SettingsService
from app.database import SessionLocal
from app.i18n import t


def user_main_menu(
    lang: str, *, is_admin: bool = False, license_title: str | None = None
) -> ReplyKeyboardMarkup:
    lic = (license_title or "").strip() or t("btn.my_licenses", lang)
    rows: list[list[KeyboardButton]] = [
        [KeyboardButton(text=t("btn.products", lang)), KeyboardButton(text=t("btn.my_orders", lang))],
        [KeyboardButton(text=t("btn.my_services", lang)), KeyboardButton(text=lic)],
        [KeyboardButton(text=t("btn.wallet", lang)), KeyboardButton(text=t("btn.account", lang))],
        [KeyboardButton(text=t("btn.tutorials", lang)), KeyboardButton(text=t("btn.support", lang))],
        # Language is NOT in the menu — the bot language is set from admin settings
        # (`bot_default_language`); users can still switch via the /language command.
        [KeyboardButton(text=t("btn.referral", lang))],
    ]
    if is_admin:
        rows.append([KeyboardButton(text=t("btn.admin_panel", lang))])
    return ReplyKeyboardMarkup(keyboard=rows, resize_keyboard=True)


async def license_section_title(lang: str) -> str:
    """The configured license-section label, falling back to the default i18n text.

    A settings-read failure must never break menu rendering, so any error falls
    back to the default label."""
    try:
        async with SessionLocal() as session:
            title = (await SettingsService(session).get_str("license_section_title", "")).strip()
    except Exception:  # noqa: BLE001 - never let a DB hiccup break the menu
        title = ""
    return title or t("btn.my_licenses", lang)


async def user_main_menu_async(lang: str, *, is_admin: bool = False) -> ReplyKeyboardMarkup:
    """Build the menu with the configured license-section label (needs a DB read)."""
    return user_main_menu(lang, is_admin=is_admin, license_title=await license_section_title(lang))

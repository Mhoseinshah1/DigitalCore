"""Canonical catalog of business settings (Phase 2).

Single source of truth for every business setting. Each entry declares its
category, value type, default, and bilingual label/description. The settings
service (app/core/settings_service.py) stores only (key, value, is_secret); this
catalog supplies the display/type metadata by key.

Categories map 1:1 to the four settings pages:
    general · telegram · payment · texts (bot messages)

Seeding (app/seed.py) inserts one row per entry with its default, never
overwriting an existing custom value. `env_var` lets an operator pre-seed an
initial value from the environment; the installer leaves those empty.
"""
from __future__ import annotations

from dataclasses import dataclass


@dataclass(frozen=True)
class SettingDef:
    key: str
    category: str
    value_type: str = "string"  # string | text | bool | int
    default: str = ""
    is_secret: bool = False
    label: str = ""
    description: str = ""
    label_fa: str = ""
    description_fa: str = ""
    env_var: str | None = None  # optional env source for the initial value


def label_for(d: SettingDef, lang: str) -> str:
    """The entry's label in the viewer's language (falls back to English/key)."""
    if lang == "fa" and d.label_fa:
        return d.label_fa
    return d.label or d.key


def description_for(d: SettingDef, lang: str) -> str:
    if lang == "fa" and d.description_fa:
        return d.description_fa
    return d.description


# Panel section metadata (title + order) keyed by category.
CATEGORIES: dict[str, dict[str, object]] = {
    "general": {"title": "General", "title_fa": "عمومی", "order": 1, "icon": "⚙️"},
    "telegram": {"title": "Telegram", "title_fa": "تلگرام", "order": 2, "icon": "✈️"},
    "payment": {"title": "Payment", "title_fa": "پرداخت", "order": 3, "icon": "💳"},
    "texts": {"title": "Bot messages", "title_fa": "پیام‌های ربات", "order": 4, "icon": "📝"},
}

# Which settings page (URL slug) renders each category.
CATEGORY_PAGE: dict[str, str] = {
    "general": "general",
    "telegram": "telegram",
    "payment": "payment",
    "texts": "bot-texts",
}


def category_title_for(category: str, lang: str) -> str:
    meta = CATEGORIES.get(category, {})
    if lang == "fa" and meta.get("title_fa"):
        return str(meta["title_fa"])
    return str(meta.get("title", category.title()))


DEFAULTS: list[SettingDef] = [
    # ---------------- General ----------------
    SettingDef("site_name", "general", "string", default="DigitalCore",
               label="Site name",
               description="Displayed name of the platform.",
               label_fa="نام سایت",
               description_fa="نام نمایشی پلتفرم."),
    SettingDef("maintenance_mode", "general", "bool", default="false",
               env_var="MAINTENANCE_MODE",
               label="Maintenance mode",
               description="When on, normal users see a maintenance notice; admins still get through.",
               label_fa="حالت تعمیر و نگهداری",
               description_fa="در حالت روشن، کاربران عادی پیام تعمیر می‌بینند؛ ادمین‌ها همچنان دسترسی دارند."),
    SettingDef("sales_enabled", "general", "bool", default="true",
               label="Sales enabled",
               description="Master switch for selling products.",
               label_fa="فعال‌بودن فروش",
               description_fa="کلید اصلی فروش محصولات."),
    SettingDef("support_enabled", "general", "bool", default="true",
               label="Support enabled",
               description="Show the support option to users.",
               label_fa="فعال‌بودن پشتیبانی",
               description_fa="نمایش گزینه پشتیبانی به کاربران."),

    # ---------------- Telegram ----------------
    SettingDef("log_group_id", "telegram", "string", env_var="LOG_GROUP_ID",
               label="Log group / channel ID",
               description="Chat ID where the bot posts logs and receipts.",
               label_fa="شناسه گروه/کانال لاگ",
               description_fa="شناسه چتی که ربات لاگ‌ها و رسیدها را در آن ارسال می‌کند."),
    SettingDef("force_join_channel", "telegram", "string", env_var="FORCE_JOIN_CHANNEL",
               label="Force-join channel",
               description="@channel users must join before using the bot.",
               label_fa="کانال عضویت اجباری",
               description_fa="@کانالی که کاربران باید پیش از استفاده از ربات عضو آن شوند."),
    SettingDef("support_username", "telegram", "string",
               label="Support username",
               description="@username users are directed to for support.",
               label_fa="نام کاربری پشتیبانی",
               description_fa="@نام‌کاربری که کاربران برای پشتیبانی به آن ارجاع می‌شوند."),

    # ---------------- Payment ----------------
    SettingDef("card_number", "payment", "string", env_var="DEFAULT_CARD_NUMBER",
               label="Card number",
               description="Destination card number for card-to-card payments.",
               label_fa="شماره کارت",
               description_fa="شماره کارت مقصد برای پرداخت کارت‌به‌کارت."),
    SettingDef("card_owner", "payment", "string", env_var="DEFAULT_CARD_OWNER",
               label="Card owner name",
               description="Full name of the card/account owner.",
               label_fa="نام صاحب کارت",
               description_fa="نام کامل صاحب کارت/حساب."),
    SettingDef("sheba_number", "payment", "string", env_var="DEFAULT_SHEBA",
               label="SHEBA / IBAN",
               description="Destination SHEBA (IBAN) number.",
               label_fa="شماره شبا",
               description_fa="شماره شبا (IBAN) مقصد."),
    SettingDef("payment_instructions", "payment", "text",
               label="Payment instructions",
               description="Instructions shown to the user when paying by card.",
               label_fa="راهنمای پرداخت",
               description_fa="راهنمایی که هنگام پرداخت کارت‌به‌کارت به کاربر نمایش داده می‌شود."),
    SettingDef("min_wallet_topup", "payment", "int", default="0",
               label="Minimum wallet top-up",
               description="Smallest allowed wallet top-up amount (toman).",
               label_fa="حداقل شارژ کیف پول",
               description_fa="کمترین مبلغ مجاز برای شارژ کیف پول (تومان)."),
    SettingDef("wallet_enabled", "payment", "bool", default="true",
               label="Wallet enabled",
               description="Allow users to keep a wallet balance.",
               label_fa="فعال‌بودن کیف پول",
               description_fa="اجازه نگهداری موجودی کیف پول برای کاربران."),
    SettingDef("max_wallet_topup", "payment", "int", default="0",
               label="Maximum wallet top-up",
               description="Largest allowed top-up amount (toman); 0 = unlimited.",
               label_fa="حداکثر شارژ کیف پول",
               description_fa="بیشترین مبلغ مجاز برای شارژ کیف پول (تومان)؛ صفر یعنی نامحدود."),
    SettingDef("wallet_topup_enabled", "payment", "bool", default="true",
               label="Wallet top-up enabled",
               description="Allow users to request wallet top-ups by receipt.",
               label_fa="فعال‌بودن شارژ کیف پول",
               description_fa="اجازه درخواست شارژ کیف پول با رسید برای کاربران."),
    SettingDef("wallet_payment_enabled", "payment", "bool", default="true",
               label="Wallet payment enabled",
               description="Allow users to pay for orders from their wallet balance.",
               label_fa="فعال‌بودن پرداخت با کیف پول",
               description_fa="اجازه پرداخت سفارش‌ها از موجودی کیف پول."),
    SettingDef("card_to_card_enabled", "payment", "bool", default="true",
               label="Card-to-card enabled",
               description="Allow manual card-to-card payments.",
               label_fa="فعال‌بودن کارت‌به‌کارت",
               description_fa="اجازه پرداخت دستی کارت‌به‌کارت."),
    SettingDef("allow_negative_wallet", "payment", "bool", default="false",
               label="Allow negative wallet",
               description="Permit admin wallet debits to push a balance below zero.",
               label_fa="اجازه موجودی منفی کیف پول",
               description_fa="اجازه بدهی که موجودی کیف پول را زیر صفر ببرد."),
    SettingDef("license_low_stock_threshold", "general", "int", default="5",
               label="License low-stock threshold",
               description="Warn when a license product's available stock falls below this.",
               label_fa="آستانه هشدار کمبود لایسنس",
               description_fa="هشدار وقتی موجودی لایسنس یک محصول کمتر از این مقدار شود."),

    # ---------------- Bot messages ----------------
    SettingDef("start_text", "texts", "text",
               label="Start message",
               description="Shown on /start.",
               label_fa="پیام شروع",
               description_fa="هنگام /start نمایش داده می‌شود."),
    SettingDef("rules_text", "texts", "text",
               label="Rules message",
               description="Rules / terms shown to users.",
               label_fa="پیام قوانین",
               description_fa="قوانین/شرایطی که به کاربران نمایش داده می‌شود."),
    SettingDef("blocked_user_text", "texts", "text",
               label="Blocked-user message",
               description="Shown to a blocked user instead of the normal menu.",
               label_fa="پیام کاربر مسدود",
               description_fa="به‌جای منوی عادی به کاربر مسدودشده نمایش داده می‌شود."),
    SettingDef("restricted_user_text", "texts", "text",
               default="حساب شما محدود شده است. برای پیگیری با پشتیبانی تماس بگیرید.",
               label="Restricted-user message",
               description="Shown to a restricted user who tries to order/buy/pay.",
               label_fa="پیام کاربر محدودشده",
               description_fa="به کاربر محدودشده هنگام تلاش برای سفارش/خرید/پرداخت نمایش داده می‌شود."),
    SettingDef("maintenance_text", "texts", "text",
               label="Maintenance message",
               description="Shown to normal users while maintenance mode is on.",
               label_fa="پیام حالت تعمیر",
               description_fa="در حالت تعمیر و نگهداری به کاربران عادی نمایش داده می‌شود."),
    SettingDef("payment_text", "texts", "text",
               label="Payment message",
               description="Bot message shown when the user starts a payment.",
               label_fa="پیام پرداخت",
               description_fa="پیام رباتی که هنگام شروع پرداخت نمایش داده می‌شود."),
    SettingDef("successful_purchase_text", "texts", "text",
               label="Successful purchase message",
               description="Sent after a purchase is approved.",
               label_fa="پیام خرید موفق",
               description_fa="پس از تأیید خرید ارسال می‌شود."),
    SettingDef("rejected_payment_text", "texts", "text",
               label="Rejected payment message",
               description="Sent when a payment is rejected.",
               label_fa="پیام رد پرداخت",
               description_fa="هنگام رد شدن پرداخت ارسال می‌شود."),
    SettingDef("support_text", "texts", "text",
               label="Support message",
               description="Shown when the user asks for support.",
               label_fa="پیام پشتیبانی",
               description_fa="هنگام درخواست پشتیبانی نمایش داده می‌شود."),
]


# Fast lookup by key.
DEFAULTS_BY_KEY: dict[str, SettingDef] = {d.key: d for d in DEFAULTS}


def keys_for_category(category: str) -> list[SettingDef]:
    """Catalog entries in a category, in declaration order."""
    return [d for d in DEFAULTS if d.category == category]

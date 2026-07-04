"""Canonical catalog of business settings.

This is the single source of truth for every business setting the platform
understands. On first boot the seeder inserts one row per entry with an
empty/default value; the admin panel (web + Telegram) edits them afterwards.

The keys the installer/settings policy explicitly requires to exist as default
records are all present here:

    card_number, sheba, card_owner, payment_text, log_group_id,
    force_join_channel, start_text, rules_text, support_text,
    sales_enabled, wallet_enabled, maintenance_mode

Labels/descriptions are bilingual (English + Persian); render via label_for /
description_for with the viewer's language. Categories map 1:1 to the sections
of the settings pages. `env_var` lets an operator pre-seed an initial value
from the environment; the installer leaves those empty.
"""
from __future__ import annotations

from dataclasses import dataclass


@dataclass(frozen=True)
class SettingDef:
    key: str
    category: str
    value_type: str = "string"  # string | text | bool | int | secret
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
    "payment": {"title": "Payment", "title_fa": "پرداخت", "order": 1, "icon": "💳"},
    "telegram": {"title": "Telegram", "title_fa": "تلگرام", "order": 2, "icon": "✈️"},
    "texts": {"title": "Bot texts", "title_fa": "متن‌های ربات", "order": 3, "icon": "📝"},
    "business": {"title": "Business", "title_fa": "کسب‌وکار", "order": 4, "icon": "⚙️"},
    "v2ray": {"title": "V2Ray", "title_fa": "V2Ray", "order": 5, "icon": "🌐"},
    "license": {"title": "License", "title_fa": "لایسنس", "order": 6, "icon": "🔑"},
}


def category_title_for(category: str, lang: str) -> str:
    meta = CATEGORIES.get(category, {})
    if lang == "fa" and meta.get("title_fa"):
        return str(meta["title_fa"])
    return str(meta.get("title", category.title()))


DEFAULTS: list[SettingDef] = [
    # ---------------- Payment ----------------
    SettingDef("card_number", "payment", "string",
               label="Card number", env_var="DEFAULT_CARD_NUMBER",
               description="Destination card number for card-to-card payments.",
               label_fa="شماره کارت",
               description_fa="شماره کارت مقصد برای پرداخت کارت‌به‌کارت."),
    SettingDef("sheba", "payment", "string",
               label="SHEBA / IBAN", env_var="DEFAULT_SHEBA",
               description="Destination SHEBA (IBAN) number.",
               label_fa="شماره شبا",
               description_fa="شماره شبا (IBAN) مقصد."),
    SettingDef("card_owner", "payment", "string",
               label="Card owner name", env_var="DEFAULT_CARD_OWNER",
               description="Full name of the card/account owner.",
               label_fa="نام صاحب کارت",
               description_fa="نام کامل صاحب کارت/حساب."),
    SettingDef("payment_text", "payment", "text",
               label="Payment instructions",
               description="Instructions shown to the user when paying.",
               label_fa="متن راهنمای پرداخت",
               description_fa="راهنمایی که هنگام پرداخت به کاربر نمایش داده می‌شود."),

    # ---------------- Telegram ----------------
    SettingDef("log_group_id", "telegram", "string",
               label="Log group / channel ID", env_var="LOG_GROUP_ID",
               description="Chat ID where the bot posts logs and receipts.",
               label_fa="شناسه گروه/کانال لاگ",
               description_fa="شناسه چتی که ربات لاگ‌ها و رسیدها را در آن ارسال می‌کند."),
    SettingDef("force_join_channel", "telegram", "string",
               label="Force-join channel", env_var="FORCE_JOIN_CHANNEL",
               description="@channel users must join before using the bot.",
               label_fa="کانال عضویت اجباری",
               description_fa="@کانالی که کاربران باید پیش از استفاده از ربات عضو آن شوند."),
    SettingDef("support_admin_username", "telegram", "string",
               label="Support admin username",
               description="@username users are directed to for support.",
               label_fa="نام کاربری پشتیبانی",
               description_fa="@نام‌کاربری که کاربران برای پشتیبانی به آن ارجاع می‌شوند."),
    SettingDef("broadcast_enabled", "telegram", "bool", default="true",
               label="Enable broadcasts",
               description="Allow the owner to broadcast messages to users.",
               label_fa="فعال‌سازی اطلاع‌رسانی",
               description_fa="اجازه ارسال پیام همگانی به کاربران."),

    # ---------------- Bot texts ----------------
    SettingDef("start_text", "texts", "text",
               label="Start message",
               description="Shown on /start.",
               label_fa="پیام شروع",
               description_fa="هنگام /start نمایش داده می‌شود."),
    SettingDef("rules_text", "texts", "text",
               label="Rules text",
               description="Rules / terms shown to users.",
               label_fa="متن قوانین",
               description_fa="قوانین/شرایطی که به کاربران نمایش داده می‌شود."),
    SettingDef("support_text", "texts", "text",
               label="Support message",
               description="Shown when the user asks for support.",
               label_fa="پیام پشتیبانی",
               description_fa="هنگام درخواست پشتیبانی نمایش داده می‌شود."),
    SettingDef("success_purchase_text", "texts", "text",
               label="Successful purchase message",
               description="Sent after a purchase is approved.",
               label_fa="پیام خرید موفق",
               description_fa="پس از تأیید خرید ارسال می‌شود."),
    SettingDef("rejected_payment_text", "texts", "text",
               label="Rejected payment message",
               description="Sent when a payment is rejected.",
               label_fa="پیام رد پرداخت",
               description_fa="هنگام رد شدن پرداخت ارسال می‌شود."),
    SettingDef("expiration_warning_text", "texts", "text",
               label="Expiration warning",
               description="Sent before a subscription expires.",
               label_fa="هشدار انقضا",
               description_fa="پیش از پایان اشتراک ارسال می‌شود."),

    # ---------------- Business ----------------
    SettingDef("sales_enabled", "business", "bool", default="true",
               label="Enable sales",
               description="Master switch for selling products.",
               label_fa="فعال‌سازی فروش",
               description_fa="کلید اصلی فروش محصولات."),
    SettingDef("card_payment_enabled", "business", "bool", default="true",
               label="Enable card-to-card payment",
               description="Allow manual card-to-card payments.",
               label_fa="فعال‌سازی کارت‌به‌کارت",
               description_fa="اجازه پرداخت دستی کارت‌به‌کارت."),
    SettingDef("wallet_enabled", "business", "bool", default="true",
               label="Enable wallet",
               description="Allow users to keep a wallet balance.",
               label_fa="فعال‌سازی کیف پول",
               description_fa="اجازه نگهداری موجودی کیف پول برای کاربران."),
    SettingDef("free_test_enabled", "business", "bool", default="false",
               label="Enable free test",
               description="Offer a free trial account.",
               label_fa="فعال‌سازی تست رایگان",
               description_fa="ارائه اکانت آزمایشی رایگان."),
    SettingDef("min_wallet_topup", "business", "int", default="0",
               label="Minimum wallet top-up",
               description="Smallest allowed wallet top-up amount.",
               label_fa="حداقل شارژ کیف پول",
               description_fa="کمترین مبلغ مجاز برای شارژ کیف پول."),
    SettingDef("maintenance_mode", "business", "bool", default="false",
               label="Maintenance mode", env_var="MAINTENANCE_MODE",
               description="Show a maintenance notice in the bot and panel.",
               label_fa="حالت تعمیر و نگهداری",
               description_fa="نمایش اعلان به‌روزرسانی در ربات و پنل."),

    # ---------------- V2Ray ----------------
    # 3X-UI server records and inbound sync are managed as dedicated resources in
    # a later phase; the selectable default inbound is stored here.
    SettingDef("default_inbound_id", "v2ray", "string",
               label="Default inbound for products",
               description="Inbound selected for newly created V2Ray products.",
               label_fa="اینباند پیش‌فرض محصولات",
               description_fa="اینباندی که برای محصولات جدید V2Ray انتخاب می‌شود."),

    # ---------------- License ----------------
    SettingDef("low_stock_threshold", "license", "int", default="5",
               label="Low-stock alert threshold",
               description="Alert the owner when license stock drops below this.",
               label_fa="آستانه هشدار کمبود موجودی",
               description_fa="وقتی موجودی لایسنس از این مقدار کمتر شود به مالک هشدار داده می‌شود."),
]


# Fast lookup by key.
DEFAULTS_BY_KEY: dict[str, SettingDef] = {d.key: d for d in DEFAULTS}

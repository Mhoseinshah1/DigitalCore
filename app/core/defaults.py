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
    SettingDef("bot_default_language", "general", "string", default="fa",
               label="Bot default language",
               description="Default bot language (fa or en) for new users. The bot "
                           "no longer asks language on /start; users can still switch "
                           "with the /language command.",
               label_fa="زبان پیش‌فرض ربات",
               description_fa="زبان پیش‌فرض ربات (fa یا en) برای کاربران جدید. ربات دیگر "
                              "هنگام /start زبان را نمی‌پرسد؛ کاربران می‌توانند با دستور "
                              "/language زبان را عوض کنند."),
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
    SettingDef("online_gateway_enabled", "payment", "bool", default="false",
               label="Online payment gateway enabled",
               description="Show the online-gateway payment button. When off the bot "
                           "shows a 'coming soon' notice; no real gateway is integrated yet.",
               label_fa="فعال‌بودن درگاه پرداخت آنلاین",
               description_fa="نمایش دکمهٔ پرداخت با درگاه آنلاین. در حالت خاموش، ربات پیام "
                              "«به‌زودی» نشان می‌دهد؛ هنوز درگاه واقعی متصل نشده است."),
    SettingDef("allow_negative_wallet", "payment", "bool", default="false",
               label="Allow negative wallet",
               description="Permit admin wallet debits to push a balance below zero.",
               label_fa="اجازه موجودی منفی کیف پول",
               description_fa="اجازه بدهی که موجودی کیف پول را زیر صفر ببرد."),

    # ------------------------------------------------------------------
    # Payment Core (invoice / manual receipt / cleanup / financial logs)
    # ------------------------------------------------------------------
    SettingDef("invoice_template_product", "texts", "text",
               default=("🧾 پیش‌فاکتور شما:\n"
                        "👤 نام کاربری: {username}\n"
                        "🌿 نام سرویس: {name_product}\n"
                        "⏳ مدت اعتبار: {Service_time} روز\n"
                        "💵 قیمت: {price} تومان\n"
                        "👥 حجم اکانت: {Volume}\n"
                        "📝 یادداشت محصول: {note}\n"
                        "💵 موجودی کیف پول شما: {userBalance}\n\n"
                        "💰 سفارش شما آماده پرداخت است."),
               label="Product pre-invoice template",
               description="Bot pre-invoice text; supports {username} {name_product} "
                           "{Service_time} {price} {Volume} {note} {userBalance} …",
               label_fa="قالب پیش‌فاکتور محصول",
               description_fa="متن پیش‌فاکتور ربات؛ متغیرها: {username} {name_product} "
                              "{Service_time} {price} {Volume} {note} {userBalance} و …"),
    SettingDef("invoice_template_wallet_topup", "texts", "text",
               default=("🧾 فاکتور شارژ کیف پول:\n"
                        "👤 نام کاربری: {username}\n"
                        "💵 مبلغ شارژ: {price} تومان\n"
                        "💰 موجودی فعلی: {userBalance}\n\n"
                        "برای پرداخت یکی از روش‌های زیر را انتخاب کنید."),
               label="Wallet top-up invoice template",
               description="Bot wallet top-up invoice text (same variables).",
               label_fa="قالب فاکتور شارژ کیف پول",
               description_fa="متن فاکتور شارژ کیف پول (همان متغیرها)."),
    SettingDef("manual_receipt_text", "texts", "text",
               default=("برای پرداخت، مبلغ {price} تومان را به شماره کارت زیر واریز کنید 👇\n\n"
                        "====================\n"
                        "{card_number}\n"
                        "{name_card}\n"
                        "====================\n\n"
                        "سپس روی «پرداخت کردم» بزنید و تصویر یا PDF رسید را ارسال کنید."),
               label="Manual receipt instructions",
               description="Card-to-card text; supports {price} {card_number} {name_card} "
                           "{tracking_code} …",
               label_fa="متن پرداخت کارت‌به‌کارت",
               description_fa="متن راهنمای کارت‌به‌کارت؛ متغیرها: {price} {card_number} "
                              "{name_card} {tracking_code} و …"),
    SettingDef("custom_gateway_enabled", "payment", "bool", default="false",
               label="Custom gateway enabled",
               description="Show the custom-gateway payment button (provider wired in a "
                           "later phase).",
               label_fa="فعال‌بودن درگاه سفارشی",
               description_fa="نمایش دکمهٔ درگاه سفارشی (اتصال واقعی در فاز بعد)."),
    SettingDef("payment_cleanup_unpaid_invoice_days", "payment", "int", default="5",
               label="Expire unpaid invoices after (days)",
               description="Unpaid invoices older than this are marked expired.",
               label_fa="انقضای فاکتورهای پرداخت‌نشده (روز)",
               description_fa="فاکتورهای پرداخت‌نشده قدیمی‌تر از این مدت منقضی می‌شوند."),
    SettingDef("payment_cleanup_pending_payment_days", "payment", "int", default="1",
               label="Expire pending payments after (days)",
               description="Pending payments (no receipt) older than this are expired.",
               label_fa="انقضای پرداخت‌های ناتمام (روز)",
               description_fa="پرداخت‌های ناتمام قدیمی‌تر از این مدت منقضی می‌شوند."),
    SettingDef("financial_log_chat_id", "telegram", "string", default="",
               label="Financial log chat id",
               description="Telegram chat that receives payment/receipt events.",
               label_fa="شناسه چت لاگ مالی",
               description_fa="چتی که رویدادهای پرداخت/رسید به آن ارسال می‌شود."),
    SettingDef("financial_log_topic_id", "telegram", "string", default="",
               label="Financial log topic id",
               description="Optional forum topic id inside the financial log chat.",
               label_fa="شناسه تاپیک لاگ مالی",
               description_fa="شناسه تاپیک (اختیاری) در چت لاگ مالی."),
    SettingDef("purchase_log_chat_id", "telegram", "string", default="",
               label="Purchase log chat id",
               description="Telegram chat that receives successful-purchase events.",
               label_fa="شناسه چت لاگ خرید",
               description_fa="چتی که رویدادهای خرید موفق به آن ارسال می‌شود."),
    SettingDef("purchase_log_topic_id", "telegram", "string", default="",
               label="Purchase log topic id",
               description="Optional forum topic id inside the purchase log chat.",
               label_fa="شناسه تاپیک لاگ خرید",
               description_fa="شناسه تاپیک (اختیاری) در چت لاگ خرید."),
    SettingDef("general_log_chat_id", "telegram", "string", default="",
               label="General log chat id",
               description="Telegram chat that receives general system events (cleanup …).",
               label_fa="شناسه چت لاگ عمومی",
               description_fa="چتی که رویدادهای عمومی سیستم (پاکسازی و …) به آن ارسال می‌شود."),
    SettingDef("general_log_topic_id", "telegram", "string", default="",
               label="General log topic id",
               description="Optional forum topic id inside the general log chat.",
               label_fa="شناسه تاپیک لاگ عمومی",
               description_fa="شناسه تاپیک (اختیاری) در چت لاگ عمومی."),
    SettingDef("license_low_stock_threshold", "general", "int", default="5",
               label="License low-stock threshold",
               description="Warn when a license product's available stock falls below this.",
               label_fa="آستانه هشدار کمبود لایسنس",
               description_fa="هشدار وقتی موجودی لایسنس یک محصول کمتر از این مقدار شود."),

    # ---------------- V2Ray lifecycle (Phase 8) ----------------
    SettingDef("v2ray_usage_refresh_enabled", "general", "bool", default="true",
               label="V2Ray usage refresh",
               description="Let the worker sync traffic usage from the panel on a schedule.",
               label_fa="به‌روزرسانی مصرف V2Ray",
               description_fa="اجازه همگام‌سازی مصرف ترافیک از پنل توسط کارگر به‌صورت زمان‌بندی‌شده."),
    SettingDef("v2ray_usage_refresh_interval_minutes", "general", "int", default="60",
               label="Usage refresh interval (minutes)",
               description="How often (minutes) each service's usage is re-synced from the panel.",
               label_fa="بازه به‌روزرسانی مصرف (دقیقه)",
               description_fa="هر چند دقیقه مصرف هر سرویس از پنل مجدداً همگام‌سازی شود."),
    SettingDef("v2ray_expiry_warning_days", "general", "int", default="3",
               label="Expiry warning (days)",
               description="Warn the user this many days before a service expires (0 = off).",
               label_fa="هشدار انقضا (روز)",
               description_fa="این تعداد روز پیش از انقضای سرویس به کاربر هشدار داده شود (۰ = خاموش)."),
    SettingDef("v2ray_traffic_warning_percent", "general", "int", default="90",
               label="Traffic warning (%)",
               description="Warn the user when used traffic reaches this percent of the quota.",
               label_fa="هشدار ترافیک (٪)",
               description_fa="وقتی مصرف ترافیک به این درصد از سهمیه رسید به کاربر هشدار داده شود."),
    SettingDef("v2ray_auto_disable_expired", "general", "bool", default="true",
               label="Auto-disable expired",
               description="Disable the panel client automatically once a service expires.",
               label_fa="غیرفعال‌سازی خودکار منقضی‌شده",
               description_fa="پس از انقضای سرویس، کلاینت پنل به‌صورت خودکار غیرفعال شود."),
    SettingDef("v2ray_auto_disable_over_quota", "general", "bool", default="true",
               label="Auto-disable over-quota",
               description="Disable the panel client automatically once a service exceeds its quota.",
               label_fa="غیرفعال‌سازی خودکار اتمام حجم",
               description_fa="پس از اتمام حجم سرویس، کلاینت پنل به‌صورت خودکار غیرفعال شود."),

    # ---------------- Support tickets & tutorials (Phase 9) ----------------
    SettingDef("ticket_attachments_enabled", "general", "bool", default="true",
               label="Ticket attachments",
               description="Allow users to attach a photo/document to support tickets.",
               label_fa="پیوست تیکت",
               description_fa="اجازه پیوست عکس/فایل به تیکت‌های پشتیبانی برای کاربران."),
    SettingDef("max_ticket_attachment_mb", "general", "int", default="10",
               label="Max ticket attachment (MB)",
               description="Largest allowed ticket attachment size in megabytes.",
               label_fa="حداکثر حجم پیوست تیکت (مگابایت)",
               description_fa="بیشترین حجم مجاز پیوست تیکت بر حسب مگابایت."),
    SettingDef("allow_reopen_closed_tickets", "general", "bool", default="true",
               label="Allow reopening tickets",
               description="Let users reopen a closed ticket instead of opening a new one.",
               label_fa="اجازه بازگشایی تیکت",
               description_fa="اجازه بازگشایی تیکت بسته‌شده به‌جای ایجاد تیکت جدید."),
    SettingDef("tutorials_enabled", "general", "bool", default="true",
               label="Tutorials enabled",
               description="Show the tutorials / knowledge base to users.",
               label_fa="فعال‌بودن آموزش‌ها",
               description_fa="نمایش بخش آموزش‌ها/راهنما به کاربران."),

    # ---------------- Marketing: coupons & referrals (Phase 10) ----------------
    SettingDef("coupons_enabled", "general", "bool", default="true",
               label="Coupons enabled",
               description="Allow discount coupons to be applied to orders.",
               label_fa="فعال‌بودن کدهای تخفیف",
               description_fa="اجازه اعمال کد تخفیف روی سفارش‌ها."),
    SettingDef("show_public_coupons", "general", "bool", default="false",
               label="Show public coupons",
               description="List active public coupons to users via /coupons.",
               label_fa="نمایش کدهای تخفیف عمومی",
               description_fa="نمایش کدهای تخفیف فعال عمومی به کاربران در /coupons."),
    SettingDef("referrals_enabled", "general", "bool", default="true",
               label="Referrals enabled",
               description="Enable the referral invite system.",
               label_fa="فعال‌بودن دعوت دوستان",
               description_fa="فعال‌سازی سیستم دعوت دوستان."),
    SettingDef("referral_reward_enabled", "general", "bool", default="true",
               label="Referral reward enabled",
               description="Grant a reward when a referred user makes a qualifying purchase.",
               label_fa="فعال‌بودن پاداش دعوت",
               description_fa="اعطای پاداش وقتی کاربر دعوت‌شده خرید واجد شرایط انجام دهد."),
    SettingDef("referral_reward_type", "general", "string", default="fixed",
               label="Referral reward type",
               description="How the reward is computed: 'fixed' (toman) or 'percent' of the order.",
               label_fa="نوع پاداش دعوت",
               description_fa="نحوه محاسبه پاداش: «fixed» (تومان) یا «percent» از مبلغ سفارش."),
    SettingDef("referral_reward_value", "general", "int", default="0",
               label="Referral reward value",
               description="Fixed toman amount, or percent, depending on the reward type.",
               label_fa="مقدار پاداش دعوت",
               description_fa="مبلغ ثابت (تومان) یا درصد، بسته به نوع پاداش."),
    SettingDef("referral_reward_requires_admin_approval", "general", "bool", default="false",
               label="Referral reward needs approval",
               description="When on, referral rewards stay pending until an admin approves them.",
               label_fa="نیاز پاداش دعوت به تأیید",
               description_fa="در حالت روشن، پاداش دعوت تا تأیید ادمین در حالت انتظار می‌ماند."),
    SettingDef("referral_reward_first_order_only", "general", "bool", default="true",
               label="Reward first order only",
               description="Only the referred user's first paid order grants a reward.",
               label_fa="پاداش فقط برای اولین سفارش",
               description_fa="فقط اولین سفارش پرداخت‌شده کاربر دعوت‌شده پاداش می‌دهد."),
    SettingDef("referral_min_order_amount", "general", "int", default="0",
               label="Referral minimum order",
               description="Minimum order amount (toman) that qualifies for a referral reward.",
               label_fa="حداقل مبلغ سفارش برای پاداش دعوت",
               description_fa="کمترین مبلغ سفارش (تومان) که واجد شرایط پاداش دعوت است."),

    # ---------------- Backup & maintenance (Phase 12) ----------------
    SettingDef("backups_enabled", "general", "bool", default="true",
               label="Backups enabled",
               description="Allow admins to create backups from the panel.",
               label_fa="فعال‌بودن پشتیبان‌گیری",
               description_fa="اجازه ایجاد پشتیبان از پنل برای ادمین‌ها."),
    SettingDef("backup_download_enabled", "general", "bool", default="true",
               label="Backup download enabled",
               description="Allow downloading backup files from the panel.",
               label_fa="فعال‌بودن دانلود پشتیبان",
               description_fa="اجازه دانلود فایل‌های پشتیبان از پنل."),
    SettingDef("scheduled_backups_enabled", "general", "bool", default="false",
               label="Scheduled backups",
               description="When on, the worker takes an automatic backup daily (off by default to avoid surprising disk use).",
               label_fa="پشتیبان‌گیری زمان‌بندی‌شده",
               description_fa="در حالت روشن، ورکر روزانه پشتیبان خودکار می‌گیرد (به‌صورت پیش‌فرض خاموش)."),
    SettingDef("scheduled_backup_type", "general", "string", default="full",
               label="Scheduled backup type",
               description="Which backup the scheduler makes: database, storage, or full.",
               label_fa="نوع پشتیبان زمان‌بندی‌شده",
               description_fa="نوع پشتیبان زمان‌بند: database یا storage یا full."),
    SettingDef("scheduled_backup_hour", "general", "int", default="3",
               label="Scheduled backup hour (UTC)",
               description="Hour of day (0-23, UTC) to run the scheduled backup.",
               label_fa="ساعت پشتیبان زمان‌بندی‌شده (UTC)",
               description_fa="ساعت شبانه‌روز (۰ تا ۲۳، UTC) برای اجرای پشتیبان زمان‌بند."),
    SettingDef("backup_retention_days", "general", "int", default="7",
               label="Backup retention (days)",
               description="Delete completed backups older than this many days (0 = keep by count only).",
               label_fa="نگه‌داری پشتیبان (روز)",
               description_fa="حذف پشتیبان‌های کامل‌شده قدیمی‌تر از این تعداد روز (۰ = فقط بر اساس تعداد)."),
    SettingDef("backup_keep_last", "general", "int", default="5",
               label="Backups to always keep",
               description="Always keep at least this many newest backups; the latest is never deleted.",
               label_fa="حداقل پشتیبان‌های نگه‌داشتنی",
               description_fa="همیشه دست‌کم این تعداد پشتیبان جدید نگه داشته می‌شود؛ آخرین پشتیبان هرگز حذف نمی‌شود."),
    SettingDef("maintenance_message", "general", "text", default="",
               label="Maintenance message",
               description="Optional note shown on the maintenance page (no secrets).",
               label_fa="پیام حالت تعمیر",
               description_fa="یادداشت اختیاری نمایش‌داده‌شده در صفحه تعمیر (بدون اطلاعات محرمانه)."),

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
    SettingDef("license_section_title", "texts", "string",
               default="لایسنس‌های من",
               label="License section title",
               description="Title of the user's license section (main-menu button, "
                           "/my_licenses header, and empty state). Set to e.g. "
                           "'اپل آیدی‌های من' to rebrand it for Apple IDs.",
               label_fa="عنوان بخش لایسنس‌ها",
               description_fa="عنوان بخش لایسنس‌های کاربر (دکمهٔ منوی اصلی، عنوان "
                              "/my_licenses و حالت خالی). مثلاً «اپل آیدی‌های من»."),
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

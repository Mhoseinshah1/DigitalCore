"""Persian (فارسی) catalog. Keys must stay identical to app/i18n/en.py."""
from __future__ import annotations

CATALOG: dict[str, str] = {
    # --- bot: general -------------------------------------------------------
    "greeting": "👋 به دیجیتال‌کور خوش آمدید!",
    "ping": "✅ pong — ربات دیجیتال‌کور فعال است.",
    "rules.empty": "ℹ️ هنوز قانونی تنظیم نشده است.",
    "maintenance.active": "🛠 ربات در حال به‌روزرسانی است. لطفاً کمی بعد دوباره تلاش کنید.",
    "main_menu": "منوی اصلی",
    "error.generic": "⚠️ خطایی رخ داد. لطفاً دوباره تلاش کنید.",

    # --- bot: user menu buttons --------------------------------------------
    "btn.products": "🛍 محصولات",
    "btn.account": "👤 حساب من",
    "btn.support": "💬 پشتیبانی",
    "btn.rules": "ℹ️ قوانین",
    "btn.admin_panel": "🛠 پنل مدیریت",
    "btn.language": "🌐 زبان / Language",

    # --- bot: admin menu buttons -------------------------------------------
    "btn.admin.dashboard": "📊 داشبورد",
    "btn.admin.users": "👥 کاربران",
    "btn.admin.broadcast": "📢 اطلاع‌رسانی",
    "btn.admin.settings": "⚙️ تنظیمات",
    "btn.admin.back": "⬅️ منوی کاربر",

    # --- bot: admin ----------------------------------------------------------
    "admin.panel_title": "🛠 پنل مدیریت — نقش: {role}",
    "admin.not_authorized": "⛔️ شما مجاز به استفاده از پنل مدیریت نیستید.",

    # --- language picker -----------------------------------------------------
    "lang.pick": "🌐 زبان خود را انتخاب کنید / Choose your language:",
    "lang.fa_label": "فارسی",
    "lang.en_label": "English",
    "lang.saved": "✅ زبان ذخیره شد.",

    # --- bot: settings editor ------------------------------------------------
    "settings.not_authorized": "⛔️ شما مجاز به مدیریت تنظیمات نیستید.",
    "settings.header": "⚙️ <b>تنظیمات</b> — یکی را برای ویرایش انتخاب کنید:",
    "settings.unknown": "تنظیم ناشناخته است.",
    "settings.prompt": (
        "✏️ <b>{label}</b>\n{description}\n\n"
        "مقدار فعلی: <code>{current}</code>\n"
        "مقدار جدید را ارسال کنید یا با /cancel لغو کنید."
    ),
    "settings.cancelled": "❌ لغو شد — چیزی تغییر نکرد.",
    "settings.invalid": "⚠️ مقدار نامعتبر: {error}\nدوباره تلاش کنید یا /cancel را بزنید.",
    "settings.updated": "✅ {label} به‌روزرسانی شد: <code>{value}</code>",
    "settings.toggled": "{label}: {state}",
    "settings.session_lost": "مشکلی پیش آمد — لطفاً دوباره ⚙️ تنظیمات را باز کنید.",
    "value.on": "روشن",
    "value.off": "خاموش",
    "value.empty": "—",
    "value.secret_set": "•••",
    "value.unset": "تنظیم نشده",

    # --- web panel -----------------------------------------------------------
    "web.panel_subtitle": "پنل مدیریت",
    "web.sign_in": "ورود",
    "web.username": "نام کاربری",
    "web.password": "رمز عبور",
    "web.invalid_credentials": "نام کاربری یا رمز عبور نادرست است.",
    "web.nav.dashboard": "داشبورد",
    "web.nav.settings": "تنظیمات",
    "web.logout": "خروج",
    "web.owner_badge": "مالک",
    "web.forbidden_title": "۴۰۳ — دسترسی مجاز نیست",
    "web.forbidden_body": "نقش شما مجوز manage_settings را ندارد.",
    "web.dash.title": "داشبورد",
    "web.dash.subtitle": "پلتفرم راه‌اندازی شده است. کسب‌وکار خود را از بخش تنظیمات پیکربندی کنید.",
    "web.dash.settings_label": "تنظیمات",
    "web.dash.note_title": "کارهایی که نصب‌کننده انجام داده",
    "web.dash.note_item1": "مدیر مالک با دسترسی به پنل وب.",
    "web.dash.note_item2": "تمام کلیدهای امنیتی به‌صورت خودکار تولید شده‌اند.",
    "web.dash.note_item3": "تنظیمات کسب‌وکار (خالی) آماده پیکربندی در همین‌جا هستند.",
    "web.dash.note_footer": (
        "کارت‌ها، کانال‌ها، پلن‌ها، سرورهای V2Ray/3X-UI، لایسنس‌ها و متن‌ها همگی "
        "در همین پنل پیکربندی می‌شوند — نصب‌کننده عمداً هیچ‌کدام را نپرسیده است."
    ),
    "web.settings.title": "تنظیمات",
    "web.settings.subtitle": "کسب‌وکار را اینجا پیکربندی کنید. این مقادیر در پایگاه‌داده ذخیره می‌شوند، نه در نصب‌کننده.",
    "web.settings.saved": "تنظیمات ذخیره شد.",
    "web.settings.not_saved": "ذخیره نشد: {error}",
    "web.settings.save_all": "ذخیره همه تنظیمات",
    "web.settings.secret_keep": "•••••••• (برای حفظ مقدار فعلی خالی بگذارید)",
    "web.settings.v2ray_note": (
        "مدیریت سرور 3X-UI (افزودن سرور · تست اتصال · همگام‌سازی اینباندها) در فاز "
        "بعدی ارائه می‌شود. انتخاب اینباند پیش‌فرض هم‌اکنون در دسترس است."
    ),
    "web.settings.license_note": (
        "افزودن محصولات لایسنس و وارد کردن موجودی در فاز بعدی ارائه می‌شود. "
        "آستانه هشدار کمبود موجودی هم‌اکنون در دسترس است."
    ),
}

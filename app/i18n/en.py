"""English catalog. Keys must stay identical to app/i18n/fa.py."""
from __future__ import annotations

CATALOG: dict[str, str] = {
    # --- bot: general -------------------------------------------------------
    "greeting": "👋 Welcome to DigitalCore!",
    "ping": "✅ pong — DigitalCore bot is running.",
    "rules.empty": "ℹ️ No rules have been configured yet.",
    "maintenance.active": "🛠 The bot is under maintenance. Please try again later.",
    "main_menu": "Main menu",
    "error.generic": "⚠️ Something went wrong. Please try again.",

    # --- bot: user menu buttons --------------------------------------------
    "btn.products": "🛍 Products",
    "btn.account": "👤 My account",
    "btn.support": "💬 Support",
    "btn.rules": "ℹ️ Rules",
    "btn.admin_panel": "🛠 Admin panel",
    "btn.language": "🌐 Language / زبان",

    # --- bot: admin menu buttons -------------------------------------------
    "btn.admin.dashboard": "📊 Dashboard",
    "btn.admin.users": "👥 Users",
    "btn.admin.broadcast": "📢 Broadcast",
    "btn.admin.settings": "⚙️ Settings",
    "btn.admin.back": "⬅️ User menu",

    # --- bot: admin ----------------------------------------------------------
    "admin.panel_title": "🛠 Admin panel — role: {role}",
    "admin.not_authorized": "⛔️ You are not authorized to use the admin panel.",

    # --- language picker -----------------------------------------------------
    "lang.pick": "🌐 Choose your language / زبان خود را انتخاب کنید:",
    "lang.fa_label": "فارسی",
    "lang.en_label": "English",
    "lang.saved": "✅ Language saved.",

    # --- bot: settings editor ------------------------------------------------
    "settings.not_authorized": "⛔️ You are not authorized to manage settings.",
    "settings.header": "⚙️ <b>Settings</b> — pick one to edit:",
    "settings.unknown": "Unknown setting.",
    "settings.prompt": (
        "✏️ <b>{label}</b>\n{description}\n\n"
        "Current: <code>{current}</code>\n"
        "Send the new value, or /cancel to abort."
    ),
    "settings.cancelled": "❌ Cancelled — nothing was changed.",
    "settings.invalid": "⚠️ Invalid value: {error}\nTry again, or /cancel.",
    "settings.updated": "✅ {label} updated to: <code>{value}</code>",
    "settings.toggled": "{label}: {state}",
    "settings.session_lost": "Something went wrong — please reopen ⚙️ Settings.",
    "value.on": "on",
    "value.off": "off",
    "value.empty": "—",
    "value.secret_set": "•••",
    "value.unset": "unset",

    # --- web panel -----------------------------------------------------------
    "web.panel_subtitle": "Admin panel",
    "web.sign_in": "Sign in",
    "web.username": "Username",
    "web.password": "Password",
    "web.invalid_credentials": "Invalid username or password.",
    "web.nav.dashboard": "Dashboard",
    "web.nav.settings": "Settings",
    "web.logout": "Log out",
    "web.owner_badge": "owner",
    "web.forbidden_title": "403 — Not allowed",
    "web.forbidden_body": "Your role does not include the manage_settings permission.",
    "web.dash.title": "Dashboard",
    "web.dash.subtitle": "The platform is booted. Configure your business from Settings.",
    "web.dash.settings_label": "Settings",
    "web.dash.note_title": "What the installer set up",
    "web.dash.note_item1": "Owner admin with web-panel access.",
    "web.dash.note_item2": "All secrets generated automatically.",
    "web.dash.note_item3": "Empty business-settings records ready to be configured here.",
    "web.dash.note_footer": (
        "Cards, channels, plans, V2Ray/3X-UI servers, licenses and texts are "
        "configured here in the panel — the installer intentionally never asked for them."
    ),
    "web.settings.title": "Settings",
    "web.settings.subtitle": "Configure the business here. These are stored in the database, not the installer.",
    "web.settings.saved": "Settings saved.",
    "web.settings.not_saved": "Not saved: {error}",
    "web.settings.save_all": "Save all settings",
    "web.settings.secret_keep": "•••••••• (leave blank to keep)",
    "web.settings.v2ray_note": (
        "3X-UI server management (add server · test connection · sync inbounds) "
        "arrives in a later phase. The default inbound selection is available now."
    ),
    "web.settings.license_note": (
        "Adding license products and importing stock arrives in a later phase. "
        "The low-stock alert threshold is available now."
    ),
}

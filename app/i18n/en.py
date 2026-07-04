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

    # --- products (shared) -----------------------------------------------------
    "product.type.license": "License",
    "product.type.v2ray": "V2Ray plan",
    "product.price_fmt": "{price} Toman",
    "product.duration_fmt": "{days} days",
    "product.traffic_fmt": "{gb} GB",
    "product.state.active": "active",
    "product.state.inactive": "inactive",
    "product.state.hidden": "hidden",

    # --- bot: user products ------------------------------------------------------
    "products.user.header": "🛍 <b>Available products:</b>",
    "products.user.empty": "No products are available yet.",

    # --- bot: admin products ------------------------------------------------------
    "btn.admin.products": "📦 Products",
    "products.admin.header": "📦 <b>Product management</b>",
    "products.admin.empty": "No products yet.",
    "products.admin.add": "➕ Add product",
    "products.not_authorized": "⛔️ You are not authorized to manage products.",
    "products.pick_type": "Choose the product type:",
    "products.ask_title": "Send the product title:",
    "products.ask_price": "Send the price in Toman (a number):",
    "products.ask_duration": "Send the duration in days (a number):",
    "products.ask_traffic": "Send the traffic in GB (a number):",
    "products.created": "✅ Product created: {title}",
    "products.updated": "✅ Updated.",
    "products.invalid_number": "⚠️ Invalid number; try again or /cancel.",
    "products.invalid": "⚠️ Invalid: {error}",
    "products.cancelled": "❌ Cancelled.",
    "products.unknown": "Product not found.",
    "products.edit_prompt": "Send the new {field} (/cancel to abort):",
    "btn.prod.toggle_active": "🔁 Toggle active",
    "btn.prod.toggle_hidden": "👁 Toggle hidden",
    "btn.prod.edit_title": "✏️ Title",
    "btn.prod.edit_price": "💰 Price",
    "btn.prod.edit_duration": "⏳ Duration",
    "btn.prod.edit_traffic": "📶 Traffic",
    "btn.prod.back": "⬅️ Back",

    # --- web: products ------------------------------------------------------------
    "web.nav.products": "Products",
    "web.products.title": "Products",
    "web.products.subtitle": "License and V2Ray product definitions. Stock and orders arrive in later phases.",
    "web.products.add": "Add product",
    "web.products.edit": "Edit",
    "web.products.empty": "No products yet — create the first one.",
    "web.products.col.title": "Title",
    "web.products.col.type": "Type",
    "web.products.col.price": "Price",
    "web.products.col.specs": "Specs",
    "web.products.col.status": "Status",
    "web.products.col.actions": "Actions",
    "web.products.form.create_title": "New product",
    "web.products.form.edit_title": "Edit product",
    "web.products.form.type": "Type",
    "web.products.form.title": "Title",
    "web.products.form.description": "Description",
    "web.products.form.price": "Price (Toman)",
    "web.products.form.duration": "Duration (days)",
    "web.products.form.traffic": "Traffic (GB)",
    "web.products.form.ip_limit": "IP limit",
    "web.products.form.server_id": "Server ID",
    "web.products.form.inbound_id": "Inbound ID",
    "web.products.form.sort_order": "Sort order",
    "web.products.form.is_active": "Active",
    "web.products.form.is_hidden": "Hidden",
    "web.products.form.save": "Save",
    "web.products.form.cancel": "Cancel",
    "web.products.form.v2ray_hint": "Duration and traffic are required for V2Ray products.",
    "web.products.saved": "Saved.",
    "web.products.not_saved": "Not saved: {error}",
    "web.products.toggle_active": "Activate/Deactivate",
    "web.products.toggle_hidden": "Show/Hide",
}

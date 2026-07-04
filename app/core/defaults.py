"""Canonical catalog of business settings.

This is the single source of truth for every business setting the platform
understands. On first boot the seeder inserts one row per entry with an
empty/default value; the admin panel edits them afterwards.

The keys the installer/settings policy explicitly requires to exist as default
records are all present here:

    card_number, sheba, card_owner, payment_text, log_group_id,
    force_join_channel, start_text, rules_text, support_text,
    sales_enabled, wallet_enabled, maintenance_mode

Categories map 1:1 to the sections of the panel Settings page. `env_var` lets an
operator pre-seed an initial value from the environment for a handful of optional
business defaults; the installer leaves those empty.
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
    env_var: str | None = None  # optional env source for the initial value


# Panel section metadata (title + order) keyed by category.
CATEGORIES: dict[str, dict[str, object]] = {
    "payment": {"title": "Payment", "order": 1, "icon": "💳"},
    "telegram": {"title": "Telegram", "order": 2, "icon": "✈️"},
    "texts": {"title": "Bot texts", "order": 3, "icon": "📝"},
    "business": {"title": "Business", "order": 4, "icon": "⚙️"},
    "v2ray": {"title": "V2Ray", "order": 5, "icon": "🌐"},
    "license": {"title": "License", "order": 6, "icon": "🔑"},
}


DEFAULTS: list[SettingDef] = [
    # ---------------- Payment ----------------
    SettingDef("card_number", "payment", "string",
               label="Card number", env_var="DEFAULT_CARD_NUMBER",
               description="Destination card number for card-to-card payments."),
    SettingDef("sheba", "payment", "string",
               label="SHEBA / IBAN", env_var="DEFAULT_SHEBA",
               description="Destination SHEBA (IBAN) number."),
    SettingDef("card_owner", "payment", "string",
               label="Card owner name", env_var="DEFAULT_CARD_OWNER",
               description="Full name of the card/account owner."),
    SettingDef("payment_text", "payment", "text",
               label="Payment instructions",
               description="Instructions shown to the user when paying."),

    # ---------------- Telegram ----------------
    SettingDef("log_group_id", "telegram", "string",
               label="Log group / channel ID", env_var="LOG_GROUP_ID",
               description="Chat ID where the bot posts logs and receipts."),
    SettingDef("force_join_channel", "telegram", "string",
               label="Force-join channel", env_var="FORCE_JOIN_CHANNEL",
               description="@channel users must join before using the bot."),
    SettingDef("support_admin_username", "telegram", "string",
               label="Support admin username",
               description="@username users are directed to for support."),
    SettingDef("broadcast_enabled", "telegram", "bool", default="true",
               label="Enable broadcasts",
               description="Allow the owner to broadcast messages to users."),

    # ---------------- Bot texts ----------------
    SettingDef("start_text", "texts", "text",
               label="Start message",
               description="Shown on /start."),
    SettingDef("rules_text", "texts", "text",
               label="Rules text",
               description="Rules / terms shown to users."),
    SettingDef("support_text", "texts", "text",
               label="Support message",
               description="Shown when the user asks for support."),
    SettingDef("success_purchase_text", "texts", "text",
               label="Successful purchase message",
               description="Sent after a purchase is approved."),
    SettingDef("rejected_payment_text", "texts", "text",
               label="Rejected payment message",
               description="Sent when a payment is rejected."),
    SettingDef("expiration_warning_text", "texts", "text",
               label="Expiration warning",
               description="Sent before a subscription expires."),

    # ---------------- Business ----------------
    SettingDef("sales_enabled", "business", "bool", default="true",
               label="Enable sales",
               description="Master switch for selling products."),
    SettingDef("card_payment_enabled", "business", "bool", default="true",
               label="Enable card-to-card payment",
               description="Allow manual card-to-card payments."),
    SettingDef("wallet_enabled", "business", "bool", default="true",
               label="Enable wallet",
               description="Allow users to keep a wallet balance."),
    SettingDef("free_test_enabled", "business", "bool", default="false",
               label="Enable free test",
               description="Offer a free trial account."),
    SettingDef("min_wallet_topup", "business", "int", default="0",
               label="Minimum wallet top-up",
               description="Smallest allowed wallet top-up amount."),
    SettingDef("maintenance_mode", "business", "bool", default="false",
               label="Maintenance mode", env_var="MAINTENANCE_MODE",
               description="Show a maintenance notice in the bot and panel."),

    # ---------------- V2Ray ----------------
    # 3X-UI server records and inbound sync are managed as dedicated resources in
    # a later phase; the selectable default inbound is stored here.
    SettingDef("default_inbound_id", "v2ray", "string",
               label="Default inbound for products",
               description="Inbound selected for newly created V2Ray products."),

    # ---------------- License ----------------
    SettingDef("low_stock_threshold", "license", "int", default="5",
               label="Low-stock alert threshold",
               description="Alert the owner when license stock drops below this."),
]


# Fast lookup by key.
DEFAULTS_BY_KEY: dict[str, SettingDef] = {d.key: d for d in DEFAULTS}

"""Shared payment UI helpers for the bot (buttons + method mapping).

Kept dependency-light (no service imports) so both the product-purchase and
wallet-top-up flows can share the manual-receipt keyboard and the method →
label/callback mapping without circular imports.
"""
from __future__ import annotations

from collections.abc import Callable

from aiogram.types import InlineKeyboardButton, InlineKeyboardMarkup

# Native "tap to copy" support landed in Bot API 7.11 / aiogram ~3.13. Detect it
# once at import so we degrade gracefully on older aiogram builds.
try:  # pragma: no cover - import-time capability probe
    from aiogram.types import CopyTextButton
    _COPY_TEXT_SUPPORTED = "copy_text" in InlineKeyboardButton.model_fields
except Exception:  # noqa: BLE001
    CopyTextButton = None  # type: ignore[assignment]
    _COPY_TEXT_SUPPORTED = False


def copy_text_supported() -> bool:
    return _COPY_TEXT_SUPPORTED


def build_copy_button(label: str, value: str, *, fallback_cb: str) -> InlineKeyboardButton:
    """A button that copies `value` to the clipboard.

    Uses Telegram's native ``copy_text`` when the running aiogram/Bot API
    supports it (the value is copied client-side, no round-trip). Otherwise it
    falls back to a callback button (`fallback_cb`) whose handler echoes the
    value in a copyable code block — we never pretend a copy happened.
    """
    if _COPY_TEXT_SUPPORTED and CopyTextButton is not None:
        return InlineKeyboardButton(text=label, copy_text=CopyTextButton(text=str(value)))
    return InlineKeyboardButton(text=label, callback_data=fallback_cb)


def manual_receipt_keyboard(
    _: Callable[..., str], *, amount: int, card_number: str,
    paid_cb: str, back_cb: str,
    copy_amount_cb: str = "paycpa", copy_card_cb: str = "paycpc",
) -> InlineKeyboardMarkup:
    """Card-to-card keyboard: copy amount, copy card, «پرداخت کردم», «بازگشت»."""
    return InlineKeyboardMarkup(inline_keyboard=[
        [
            build_copy_button(_("purchase.btn.copy_amount"), str(int(amount)),
                              fallback_cb=copy_amount_cb),
            build_copy_button(_("purchase.btn.copy_card"), card_number,
                              fallback_cb=copy_card_cb),
        ],
        [InlineKeyboardButton(text=_("purchase.btn.paid"), callback_data=paid_cb)],
        [InlineKeyboardButton(text=_("btn.back"), callback_data=back_cb)],
    ])


# Payment method_type → invoice-button label i18n key. wallet/manual/online keep
# their legacy keys (locked in by tests); the rest use their own keys.
METHOD_LABEL_KEY: dict[str, str] = {
    "wallet": "btn.pay_wallet",
    "manual_receipt": "btn.pay_card",
    "online_gateway": "btn.pay_gateway",
    "custom_gateway": "btn.pay_custom",
    "crypto": "btn.pay_crypto",
    "telegram_stars": "btn.pay_stars",
}

# Gateway-ish method types that are not wired to a real provider yet. Tapping
# them shows a safe Persian "not connected" notice instead of dead-ending.
UNIMPLEMENTED_GATEWAY_TYPES: frozenset[str] = frozenset(
    {"online_gateway", "custom_gateway", "crypto", "telegram_stars"}
)


def method_label(_: Callable[..., str], method_type: str, fallback_title: str = "") -> str:
    key = METHOD_LABEL_KEY.get(method_type)
    if key:
        text = _(key)
        if text != key:
            return text
    return fallback_title or method_type

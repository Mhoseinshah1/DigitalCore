"""Best-effort Telegram notifications to admins/log-group.

Failures here MUST NOT break the user flow — every send is wrapped and any error
is logged and swallowed. Recipients are the main owner admin
(settings.TELEGRAM_ADMIN_ID) and, if configured, the log group (log_group_id).
"""
from __future__ import annotations

import logging

from aiogram import Bot
from aiogram.types import InlineKeyboardButton, InlineKeyboardMarkup

from app.config import settings
from app.core.settings_service import SettingsService
from app.database import SessionLocal
from app.i18n import t

log = logging.getLogger("bot.notify")

CB_NEXT_PHASE = "receipt_next_phase"  # placeholder button (approve/reject in P4)


async def _recipients() -> list[int | str]:
    targets: list[int | str] = []
    if settings.TELEGRAM_ADMIN_ID:
        targets.append(settings.TELEGRAM_ADMIN_ID)
    try:
        async with SessionLocal() as session:
            raw = (await SettingsService(session).get_str("log_group_id", "")).strip()
    except Exception as exc:  # noqa: BLE001 - notification must never raise
        log.warning("Could not read log_group_id: %s", exc)
        raw = ""
    if raw:
        try:
            targets.append(int(raw))
        except ValueError:
            targets.append(raw)  # @channelusername form
    # De-dupe while preserving order.
    seen: set[str] = set()
    unique: list[int | str] = []
    for t_ in targets:
        key = str(t_)
        if key not in seen:
            seen.add(key)
            unique.append(t_)
    return unique


def _build_text(order, payment, product, user, lang: str) -> str:
    username = user.username or user.first_name or "—"
    submitted = payment.submitted_at.strftime("%Y-%m-%d %H:%M") if payment.submitted_at else "—"
    return "\n".join([
        t("notify.receipt.title", lang),
        "",
        t("notify.receipt.order", lang, number=order.order_number),
        t("notify.receipt.user", lang, username=username, tg_id=user.telegram_id or "—"),
        t("notify.receipt.product", lang, title=product.title,
          type=t(f"product.type.{product.type}", lang)),
        t("notify.receipt.amount", lang, amount=f"{order.final_amount:,}"),
        t("notify.receipt.time", lang, time=submitted),
        "",
        t("notify.receipt.review_panel", lang),
        t("notify.receipt.next_phase", lang),
    ])


async def notify_receipt_submitted(
    bot: Bot, *, order, payment, product, user, lang: str = "fa"
) -> None:
    """Post the new-receipt notice (with the receipt file when possible)."""
    text = _build_text(order, payment, product, user, lang)
    keyboard = InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text=t("notify.receipt.next_phase", lang),
                              callback_data=CB_NEXT_PHASE)],
    ])
    file_id = payment.receipt_file_id
    mime = (payment.receipt_mime_type or "").lower()
    for chat_id in await _recipients():
        try:
            if file_id and mime.startswith("image/"):
                await bot.send_photo(chat_id, file_id, caption=text,
                                     parse_mode="HTML", reply_markup=keyboard)
            elif file_id:
                await bot.send_document(chat_id, file_id, caption=text,
                                        parse_mode="HTML", reply_markup=keyboard)
            else:
                await bot.send_message(chat_id, text, parse_mode="HTML",
                                       reply_markup=keyboard)
        except Exception as exc:  # noqa: BLE001 - never break the user's flow
            log.warning("Failed to notify %s about receipt %s: %s",
                        chat_id, payment.id, exc)

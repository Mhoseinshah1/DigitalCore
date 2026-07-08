"""Best-effort financial/purchase/general event logs to Telegram (Payment Core).

Admins configure a chat id (and optional forum topic id) per log stream in
settings; every event send is fire-and-forget — a missing bot, an unset chat,
or a Telegram error is swallowed with a warning so it can NEVER break a payment
flow. Messages contain amounts + tracking codes only — no tokens, no receipts'
bytes, no card PANs beyond what admins configured to show users anyway.
"""
from __future__ import annotations

import logging
from datetime import datetime, timezone

from sqlalchemy.ext.asyncio import AsyncSession

from app.core.settings_service import SettingsService

log = logging.getLogger("financial_log")

FINANCIAL = "financial"
PURCHASE = "purchase"
GENERAL = "general"

_SETTING_FOR_STREAM = {
    FINANCIAL: ("financial_log_chat_id", "financial_log_topic_id"),
    PURCHASE: ("purchase_log_chat_id", "purchase_log_topic_id"),
    GENERAL: ("general_log_chat_id", "general_log_topic_id"),
}


def _now_str() -> str:
    return datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M UTC")


async def send_log(
    session: AsyncSession, bot, stream: str, text: str
) -> bool:
    """Send `text` to the configured chat/topic for `stream`. Never raises."""
    if bot is None or not text:
        return False
    chat_key, topic_key = _SETTING_FOR_STREAM.get(stream, _SETTING_FOR_STREAM[GENERAL])
    try:
        svc = SettingsService(session)
        chat_id = (await svc.get_str(chat_key, "")).strip()
        if not chat_id:
            return False
        topic_raw = (await svc.get_str(topic_key, "")).strip()
        kwargs: dict = {}
        if topic_raw.lstrip("-").isdigit() and int(topic_raw):
            kwargs["message_thread_id"] = int(topic_raw)
        await bot.send_message(chat_id=chat_id, text=text, **kwargs)
        return True
    except Exception as exc:  # noqa: BLE001 - logging must never break payments
        log.warning("%s log send failed: %s", stream, exc)
        return False


# ---------------------------------------------------------------------------
# Event helpers — one-line composers for the events the payment core emits.
# ---------------------------------------------------------------------------
async def log_new_payment(session, bot, *, tracking_code, amount, method,
                          user_label) -> bool:
    return await send_log(session, bot, FINANCIAL,
        f"💳 پرداخت جدید\nکد پیگیری: {tracking_code}\nمبلغ: {amount:,} تومان\n"
        f"روش: {method}\nکاربر: {user_label}\n🕓 {_now_str()}")


async def log_receipt_submitted(session, bot, *, tracking_code, amount,
                                user_label) -> bool:
    return await send_log(session, bot, FINANCIAL,
        f"🧾 رسید جدید در انتظار بررسی\nکد پیگیری: {tracking_code}\n"
        f"مبلغ: {amount:,} تومان\nکاربر: {user_label}\n🕓 {_now_str()}")


async def log_receipt_approved(session, bot, *, tracking_code, amount,
                               admin_label) -> bool:
    return await send_log(session, bot, FINANCIAL,
        f"✅ رسید تایید شد\nکد پیگیری: {tracking_code}\nمبلغ: {amount:,} تومان\n"
        f"ادمین: {admin_label}\n🕓 {_now_str()}")


async def log_receipt_rejected(session, bot, *, tracking_code, amount,
                               admin_label, reason) -> bool:
    return await send_log(session, bot, FINANCIAL,
        f"❌ رسید رد شد\nکد پیگیری: {tracking_code}\nمبلغ: {amount:,} تومان\n"
        f"ادمین: {admin_label}\nدلیل: {reason}\n🕓 {_now_str()}")


async def log_wallet_topped_up(session, bot, *, tracking_code, amount, bonus,
                               user_label) -> bool:
    extra = f" (+{bonus:,} پاداش)" if bonus else ""
    return await send_log(session, bot, FINANCIAL,
        f"👛 کیف پول شارژ شد\nکد پیگیری: {tracking_code}\n"
        f"مبلغ: {amount:,} تومان{extra}\nکاربر: {user_label}\n🕓 {_now_str()}")


async def log_product_purchased(session, bot, *, order_number, product_title,
                                amount, user_label) -> bool:
    return await send_log(session, bot, PURCHASE,
        f"🛍 خرید موفق\nسفارش: {order_number}\nمحصول: {product_title}\n"
        f"مبلغ: {amount:,} تومان\nکاربر: {user_label}\n🕓 {_now_str()}")


async def log_provisioning_error(session, bot, *, order_number, reason) -> bool:
    return await send_log(session, bot, GENERAL,
        f"⚠️ خطای تحویل/ساخت سرویس پس از پرداخت\nسفارش: {order_number}\n"
        f"علت: {reason}\nپرداخت معتبر باقی می‌ماند؛ لطفاً از پنل پیگیری کنید.\n"
        f"🕓 {_now_str()}")


async def log_cleanup_summary(session, bot, *, invoice_days, count_invoice,
                              payment_days, count_payment) -> bool:
    return await send_log(session, bot, GENERAL,
        "🧹 پاکسازی خودکار سیستم\n\n"
        f"📄 فاکتورهای پرداخت‌نشده‌ی قدیمی‌تر از {invoice_days} روز حذف/منقضی شدند.\n"
        f"└ تعداد: {count_invoice}\n\n"
        f"💳 پرداخت‌های ناتمام قدیمی‌تر از {payment_days} روز منقضی شدند.\n"
        f"└ تعداد: {count_payment}\n\n"
        f"🕓 زمان اجرا: {_now_str()}")

"""Scheduled cleanup of stale invoices/payments (Payment Core).

Unpaid invoices and pending (no-receipt) payments older than their configured
age are marked `expired` — never deleted, and paid / rejected / cancelled /
receipt-submitted rows are never touched. A Persian summary goes to the general
log stream (best-effort). Runs from the worker's hourly maintenance sweep; can
also be invoked manually:  python -c "…run_payment_cleanup…" (see README).
"""
from __future__ import annotations

import logging

from sqlalchemy.ext.asyncio import AsyncSession

from app.core.settings_service import SettingsService
from app.services import financial_log_service, payment_core_service

log = logging.getLogger("payment_cleanup")


async def cleanup_unpaid_invoices(session: AsyncSession) -> tuple[int, int]:
    """Expire unpaid invoices older than the configured days. -> (count, days)"""
    days = await SettingsService(session).get_int(
        "payment_cleanup_unpaid_invoice_days", 5)
    count = await payment_core_service.expire_old_unpaid_invoices(session, days)
    return count, days


async def cleanup_pending_payments(session: AsyncSession) -> tuple[int, int]:
    """Expire pending payments older than the configured days. -> (count, days)"""
    days = await SettingsService(session).get_int(
        "payment_cleanup_pending_payment_days", 1)
    count = await payment_core_service.expire_old_pending_payments(session, days)
    return count, days


async def run_payment_cleanup(session: AsyncSession, *, bot=None) -> dict:
    """Run both sweeps and (best-effort) send the Persian summary log."""
    count_invoice, invoice_days = await cleanup_unpaid_invoices(session)
    count_payment, payment_days = await cleanup_pending_payments(session)
    result = {
        "invoices_expired": count_invoice, "invoice_days": invoice_days,
        "payments_expired": count_payment, "payment_days": payment_days,
        "acted": bool(count_invoice or count_payment),
    }
    if result["acted"]:
        log.info("payment cleanup: %s", result)
        await financial_log_service.log_cleanup_summary(
            session, bot, invoice_days=invoice_days, count_invoice=count_invoice,
            payment_days=payment_days, count_payment=count_payment,
        )
    return result

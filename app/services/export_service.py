"""CSV exports (Phase 11): admin-only, secret-free tabular dumps.

Every export returns a UTF-8 string with a leading BOM (``﻿``) so Excel on
Windows opens Persian/Unicode content in the right encoding. Content is built
in memory with the stdlib ``csv`` module — fine at the current scale — and each
query is bounded by ``MAX_ROWS`` so a runaway table can never exhaust memory
(the cap is logged, never silent).

Security invariants enforced here (see Phase 11 spec):
  * license ``password`` is NEVER exported (no route, no column);
  * V2Ray ``client_uuid`` is masked and ``subscription_url`` / ``sub_id`` are
    dropped — no working credential leaves the panel;
  * no XUI credentials, bot token, or secret keys anywhere;
  * users export omits ``phone_number`` and internal admin notes.
"""
from __future__ import annotations

import csv
import io
import logging
from datetime import datetime, timezone
from typing import Any

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.models.license_item import LicenseItem
from app.models.order import Order
from app.models.payment import Payment
from app.models.product import Product
from app.models.user import User
from app.models.v2ray_service import V2RayService
from app.models.wallet_transaction import WalletTransaction
from app.services.report_service import (
    COUPONS_AVAILABLE,
    REFERRALS_AVAILABLE,
    TICKETS_AVAILABLE,
    _range_clauses,
)

if COUPONS_AVAILABLE:
    from app.models.coupon import Coupon
    from app.models.coupon_usage import CouponUsage
if REFERRALS_AVAILABLE:
    from app.models.referral_reward import ReferralReward
if TICKETS_AVAILABLE:
    from app.models.ticket import Ticket

log = logging.getLogger("reports.export")

BOM = "﻿"
MAX_ROWS = 100_000  # in-memory safety cap; truncation is logged, never silent


# ==========================================================================
# Helpers
# ==========================================================================
def _fmt(value: Any) -> str:
    """CSV-cell formatting: datetimes → ISO, None → '', bools → true/false."""
    if value is None:
        return ""
    if isinstance(value, datetime):
        return value.isoformat()
    if isinstance(value, bool):
        return "true" if value else "false"
    return str(value)


def _render(headers: list[str], rows: list[list[Any]]) -> str:
    buf = io.StringIO()
    buf.write(BOM)
    writer = csv.writer(buf)
    writer.writerow(headers)
    for row in rows:
        writer.writerow([_fmt(c) for c in row])
    return buf.getvalue()


def export_filename(name: str) -> str:
    """Date-stamped, filesystem-safe filename, e.g. ``orders-2026-07-08.csv``."""
    stamp = datetime.now(timezone.utc).strftime("%Y-%m-%d")
    safe = "".join(c if c.isalnum() or c in "-_" else "-" for c in name)
    return f"{safe}-{stamp}.csv"


def _mask_uuid(value: str | None) -> str:
    """Keep only the last 4 chars so a UUID is recognisable but not usable."""
    if not value:
        return ""
    return "****" + value[-4:] if len(value) > 4 else "****"


async def _fetch(session: AsyncSession, stmt) -> list:
    rows = (await session.execute(stmt.limit(MAX_ROWS + 1))).all()
    if len(rows) > MAX_ROWS:
        log.warning("CSV export truncated at %d rows for %s", MAX_ROWS, stmt)
        rows = rows[:MAX_ROWS]
    return rows


# ==========================================================================
# Exports
# ==========================================================================
async def export_orders_csv(session: AsyncSession, start_date=None, end_date=None, status: str | None = None) -> str:  # noqa: ANN001,E501
    headers = ["id", "order_number", "user_id", "product", "product_type", "status",
               "action_type", "amount", "discount_amount", "final_amount",
               "payment_method", "coupon_code", "created_at", "delivered_at"]
    stmt = (
        select(Order, Product.title, Product.type)
        .join(Product, Product.id == Order.product_id, isouter=True)
        .where(*_range_clauses(Order.created_at, start_date, end_date))
        .order_by(Order.id.asc())
    )
    if status:
        stmt = stmt.where(Order.status == status)
    rows = [
        [o.id, o.order_number, o.user_id, title, ptype, o.status, o.action_type,
         o.amount, o.discount_amount, o.final_amount, o.payment_method,
         o.coupon_code, o.created_at, o.delivered_at]
        for o, title, ptype in await _fetch(session, stmt)
    ]
    return _render(headers, rows)


async def export_payments_csv(session: AsyncSession, start_date=None, end_date=None, method: str | None = None, status: str | None = None) -> str:  # noqa: ANN001,E501
    headers = ["id", "order_id", "user_id", "amount", "method", "status",
               "tracking_code", "refunded_amount", "created_at", "approved_at"]
    stmt = (
        select(Payment)
        .where(*_range_clauses(Payment.created_at, start_date, end_date))
        .order_by(Payment.id.asc())
    )
    if method:
        stmt = stmt.where(Payment.method == method)
    if status:
        stmt = stmt.where(Payment.status == status)
    rows = [
        [p.id, p.order_id, p.user_id, p.amount, p.method, p.status,
         p.tracking_code, p.refunded_amount, p.created_at, p.approved_at]
        for (p,) in await _fetch(session, stmt)
    ]
    return _render(headers, rows)


async def export_wallet_transactions_csv(session: AsyncSession, start_date=None, end_date=None) -> str:  # noqa: ANN001,E501
    headers = ["id", "user_id", "amount", "balance_before", "balance_after", "type",
               "status", "reason", "order_id", "payment_id", "topup_id", "created_at"]
    stmt = (
        select(WalletTransaction)
        .where(*_range_clauses(WalletTransaction.created_at, start_date, end_date))
        .order_by(WalletTransaction.id.asc())
    )
    rows = [
        [t.id, t.user_id, t.amount, t.balance_before, t.balance_after, t.type,
         t.status, t.reason, t.order_id, t.payment_id, t.topup_id, t.created_at]
        for (t,) in await _fetch(session, stmt)
    ]
    return _render(headers, rows)


async def export_users_csv(session: AsyncSession, start_date=None, end_date=None) -> str:  # noqa: ANN001,E501
    # No phone_number, no admin_note, no restriction_reason — non-financial,
    # non-secret profile columns only.
    headers = ["id", "telegram_id", "username", "first_name", "last_name",
               "wallet_balance", "is_blocked", "is_restricted", "is_verified",
               "language", "referral_code", "created_at", "last_activity_at"]
    stmt = (
        select(User)
        .where(*_range_clauses(User.created_at, start_date, end_date))
        .order_by(User.id.asc())
    )
    rows = [
        [u.id, u.telegram_id, u.username, u.first_name, u.last_name, u.wallet_balance,
         u.is_blocked, u.is_restricted, u.is_verified, u.language, u.referral_code,
         u.created_at, u.last_activity_at]
        for (u,) in await _fetch(session, stmt)
    ]
    return _render(headers, rows)


async def export_products_csv(session: AsyncSession) -> str:
    headers = ["id", "type", "title", "price", "duration_days", "traffic_gb",
               "ip_limit", "action_type", "is_active", "is_hidden", "stock_count",
               "sort_order", "created_at"]
    stmt = select(Product).order_by(Product.id.asc())
    rows = [
        [p.id, p.type, p.title, p.price, p.duration_days, p.traffic_gb, p.ip_limit,
         p.action_type, p.is_active, p.is_hidden, p.stock_count, p.sort_order,
         p.created_at]
        for (p,) in await _fetch(session, stmt)
    ]
    return _render(headers, rows)


async def export_licenses_csv(session: AsyncSession, status: str | None = None) -> str:
    # password is intentionally NOT selected or emitted.
    headers = ["id", "product", "email", "status", "sold_to_user_id", "order_id",
               "sold_at", "created_at"]
    stmt = (
        select(LicenseItem, Product.title)
        .join(Product, Product.id == LicenseItem.product_id, isouter=True)
        .order_by(LicenseItem.id.asc())
    )
    if status:
        stmt = stmt.where(LicenseItem.status == status)
    rows = [
        [lic.id, title, lic.email, lic.status, lic.sold_to_user_id, lic.order_id,
         lic.sold_at, lic.created_at]
        for lic, title in await _fetch(session, stmt)
    ]
    return _render(headers, rows)


async def export_v2ray_services_csv(session: AsyncSession, status: str | None = None) -> str:
    # client_uuid masked; subscription_url / sub_id / qr_code_path never exported.
    headers = ["id", "user_id", "product", "status", "client_email", "client_uuid_masked",
               "total_bytes", "used_bytes", "expire_at", "created_at"]
    stmt = (
        select(V2RayService, Product.title)
        .join(Product, Product.id == V2RayService.product_id, isouter=True)
        .order_by(V2RayService.id.asc())
    )
    if status:
        stmt = stmt.where(V2RayService.status == status)
    rows = [
        [s.id, s.user_id, title, s.status, s.client_email, _mask_uuid(s.client_uuid),
         s.total_gb, s.used_gb, s.expire_at, s.created_at]
        for s, title in await _fetch(session, stmt)
    ]
    return _render(headers, rows)


async def export_coupon_usages_csv(session: AsyncSession, start_date=None, end_date=None) -> str:  # noqa: ANN001,E501
    headers = ["id", "coupon_code", "user_id", "order_id", "discount_amount", "created_at"]
    if not COUPONS_AVAILABLE:
        return _render(headers, [])
    stmt = (
        select(CouponUsage, Coupon.code)
        .join(Coupon, Coupon.id == CouponUsage.coupon_id, isouter=True)
        .where(*_range_clauses(CouponUsage.created_at, start_date, end_date))
        .order_by(CouponUsage.id.asc())
    )
    rows = [
        [cu.id, code, cu.user_id, cu.order_id, cu.discount_amount, cu.created_at]
        for cu, code in await _fetch(session, stmt)
    ]
    return _render(headers, rows)


async def export_referral_rewards_csv(session: AsyncSession, start_date=None, end_date=None) -> str:  # noqa: ANN001,E501
    headers = ["id", "referrer_user_id", "referred_user_id", "order_id", "reward_type",
               "reward_amount", "status", "created_at", "paid_at"]
    if not REFERRALS_AVAILABLE:
        return _render(headers, [])
    stmt = (
        select(ReferralReward)
        .where(*_range_clauses(ReferralReward.created_at, start_date, end_date))
        .order_by(ReferralReward.id.asc())
    )
    rows = [
        [r.id, r.referrer_user_id, r.referred_user_id, r.order_id, r.reward_type,
         r.reward_amount, r.status, r.created_at, r.paid_at]
        for (r,) in await _fetch(session, stmt)
    ]
    return _render(headers, rows)


async def export_tickets_csv(session: AsyncSession, start_date=None, end_date=None) -> str:  # noqa: ANN001,E501
    headers = ["id", "ticket_number", "user_id", "subject", "status", "priority",
               "assigned_admin_id", "created_at", "closed_at"]
    if not TICKETS_AVAILABLE:
        return _render(headers, [])
    stmt = (
        select(Ticket)
        .where(*_range_clauses(Ticket.created_at, start_date, end_date))
        .order_by(Ticket.id.asc())
    )
    rows = [
        [t.id, t.ticket_number, t.user_id, t.subject, t.status, t.priority,
         t.assigned_admin_id, t.created_at, t.closed_at]
        for (t,) in await _fetch(session, stmt)
    ]
    return _render(headers, rows)

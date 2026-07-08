"""Reports & analytics (Phase 11): read-only SQL aggregation over the domain.

Everything here is a *read* — no row is mutated. Queries use SQL aggregation
(`func.count` / `func.sum` / `group_by`) rather than loading rows into memory, so
they stay cheap as the tables grow. Money is integer toman throughout (the
platform convention); there is no Decimal money anywhere.

Optional Phase 9/10 models (tickets, coupons, referrals) are imported
defensively: if a checkout predates those phases the import fails, the matching
``*_AVAILABLE`` flag is False, and the related report functions return
``{"available": False}`` instead of crashing.

Day buckets use ``func.date(col)`` which is portable across SQLite (tests) and
PostgreSQL (runtime); the bucket value is normalised to a ``YYYY-MM-DD`` string
before it leaves this module.
"""
from __future__ import annotations

from datetime import date, datetime, time, timedelta, timezone
from typing import Any

from sqlalchemy import case, func, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.settings_service import SettingsService
from app.models.license_item import LicenseItem
from app.models.order import Order
from app.models.payment import Payment
from app.models.product import Product
from app.models.user import User
from app.models.v2ray_service import V2RayService
from app.models.wallet_topup import WalletTopupRequest
from app.models.wallet_transaction import WalletTransaction

# --- Optional models (Phase 9/10). Absence must degrade gracefully. ---------
try:
    from app.models.coupon import Coupon
    from app.models.coupon_usage import CouponUsage
    COUPONS_AVAILABLE = True
except ImportError:  # pragma: no cover - defensive
    Coupon = CouponUsage = None  # type: ignore
    COUPONS_AVAILABLE = False

try:
    from app.models.referral_reward import ReferralReward
    REFERRALS_AVAILABLE = True
except ImportError:  # pragma: no cover - defensive
    ReferralReward = None  # type: ignore
    REFERRALS_AVAILABLE = False

try:
    from app.models.ticket import Ticket
    TICKETS_AVAILABLE = True
except ImportError:  # pragma: no cover - defensive
    Ticket = None  # type: ignore
    TICKETS_AVAILABLE = False

UTC = timezone.utc

# A completed sale (revenue recognised) is a delivered order; card/wallet money
# is "received" once its payment is approved.
REVENUE_ORDER_STATUSES: tuple[str, ...] = ("delivered",)
PAID_PAYMENT_STATUSES: tuple[str, ...] = ("approved",)
PENDING_ORDER_STATUSES: tuple[str, ...] = (
    "pending_payment", "waiting_admin", "approved", "provisioning_pending",
)
OPEN_TICKET_STATUSES: tuple[str, ...] = ("open", "pending_admin", "pending_user")

DATE_PRESETS: tuple[str, ...] = (
    "today", "yesterday", "last_7_days", "last_30_days", "this_month", "last_month",
)


# ==========================================================================
# Date handling
# ==========================================================================
def _midnight(d: date) -> datetime:
    """00:00 UTC on the given calendar day."""
    return datetime.combine(d, time.min, tzinfo=UTC)


def _parse_day(value: Any) -> date | None:
    """Coerce None / date / datetime / 'YYYY-MM-DD' into a calendar date."""
    if value is None or value == "":
        return None
    if isinstance(value, datetime):
        return value.date()
    if isinstance(value, date):
        return value
    return date.fromisoformat(str(value).strip()[:10])


def _add_month(d: date) -> date:
    """First day of the month after ``d``'s month."""
    return date(d.year + (d.month == 12), 1 if d.month == 12 else d.month + 1, 1)


def _prev_month(first_of_month: date) -> date:
    """First day of the month before the given first-of-month date."""
    prev_last = first_of_month - timedelta(days=1)
    return prev_last.replace(day=1)


def parse_date_range(
    start_date: Any = None, end_date: Any = None, preset: str | None = None,
) -> tuple[datetime | None, datetime | None]:
    """Return a half-open ``[start, end)`` UTC window from a preset or dates.

    Presets: today, yesterday, last_7_days, last_30_days, this_month,
    last_month. For a custom range the ``end_date`` day is *inclusive* — the
    returned ``end`` is midnight of the following day so ``col < end`` keeps the
    whole end day. Any bound may be None (open-ended). An unknown preset yields
    ``(None, None)``.
    """
    if preset:
        today = datetime.now(UTC).date()
        preset = preset.lower()
        if preset == "today":
            return _midnight(today), _midnight(today + timedelta(days=1))
        if preset == "yesterday":
            return _midnight(today - timedelta(days=1)), _midnight(today)
        if preset == "last_7_days":
            return _midnight(today - timedelta(days=6)), _midnight(today + timedelta(days=1))
        if preset == "last_30_days":
            return _midnight(today - timedelta(days=29)), _midnight(today + timedelta(days=1))
        if preset == "this_month":
            first = today.replace(day=1)
            return _midnight(first), _midnight(_add_month(first))
        if preset == "last_month":
            first = today.replace(day=1)
            return _midnight(_prev_month(first)), _midnight(first)
        return None, None

    s_day = _parse_day(start_date)
    e_day = _parse_day(end_date)
    start = _midnight(s_day) if s_day else None
    end = _midnight(e_day + timedelta(days=1)) if e_day else None
    return start, end


def get_previous_period(
    start_date: datetime | None, end_date: datetime | None,
) -> tuple[datetime | None, datetime | None]:
    """The equal-length window immediately before ``[start, end)``."""
    if start_date is None or end_date is None:
        return None, None
    span = end_date - start_date
    return start_date - span, start_date


def safe_percent_change(current: float, previous: float) -> float | None:
    """Percent change current-vs-previous; None when there is no baseline.

    ``previous == 0`` → 0.0 if current is also 0, else None ("n/a — no base").
    """
    if previous == 0:
        return 0.0 if current == 0 else None
    return round((current - previous) / previous * 100.0, 2)


# ==========================================================================
# Small query helpers
# ==========================================================================
def _between(stmt, col, start: datetime | None, end: datetime | None):
    if start is not None:
        stmt = stmt.where(col >= start)
    if end is not None:
        stmt = stmt.where(col < end)
    return stmt


async def _count(session: AsyncSession, model, *where) -> int:
    stmt = select(func.count()).select_from(model)
    for clause in where:
        stmt = stmt.where(clause)
    return int(await session.scalar(stmt) or 0)


async def _sum(session: AsyncSession, col, *where) -> int:
    stmt = select(func.coalesce(func.sum(col), 0))
    for clause in where:
        stmt = stmt.where(clause)
    return int(await session.scalar(stmt) or 0)


def _daystr(value: Any) -> str:
    """Normalise a ``func.date`` bucket (date on PG, str on SQLite) to text."""
    return str(value)[:10] if value is not None else ""


def _fill_days(rows: dict[str, dict], start: datetime | None, end: datetime | None,
               template: dict) -> list[dict]:
    """Return a per-day series. If ``[start, end)`` is bounded and <= 366 days,
    fill missing days with zeros so charts get a contiguous x-axis; otherwise
    just return the present days sorted ascending."""
    if start is not None and end is not None:
        span = (end.date() - start.date()).days
        if 0 <= span <= 366:
            out: list[dict] = []
            cur = start.date()
            while cur < end.date():
                key = cur.isoformat()
                out.append(rows.get(key) or {"date": key, **template})
                cur += timedelta(days=1)
            return out
    return [rows[k] for k in sorted(rows)]


# ==========================================================================
# Dashboard summaries
# ==========================================================================
async def get_revenue_summary(session: AsyncSession, start_date=None, end_date=None) -> dict:  # noqa: ANN001,E501
    """Recognised revenue (delivered orders) for the window + prev-period change."""
    total = await _sum(session, Order.final_amount,
                       Order.status.in_(REVENUE_ORDER_STATUSES),
                       *(_range_clauses(Order.delivered_at, start_date, end_date)))
    orders = await _count(session, Order,
                          Order.status.in_(REVENUE_ORDER_STATUSES),
                          *(_range_clauses(Order.delivered_at, start_date, end_date)))
    p_start, p_end = get_previous_period(start_date, end_date)
    prev_total = await _sum(session, Order.final_amount,
                            Order.status.in_(REVENUE_ORDER_STATUSES),
                            *(_range_clauses(Order.delivered_at, p_start, p_end)))
    return {
        "total": total,
        "orders": orders,
        "avg_order_value": (total // orders) if orders else 0,
        "previous_total": prev_total,
        "change_pct": safe_percent_change(total, prev_total),
    }


def _range_clauses(col, start: datetime | None, end: datetime | None) -> list:
    clauses = []
    if start is not None:
        clauses.append(col >= start)
    if end is not None:
        clauses.append(col < end)
    return clauses


async def get_order_summary(session: AsyncSession, start_date=None, end_date=None) -> dict:  # noqa: ANN001,E501
    """Order counts (by created_at) + a status breakdown for the window."""
    rc = _range_clauses(Order.created_at, start_date, end_date)
    total = await _count(session, Order, *rc)
    by_status = await get_orders_by_status(session, start_date, end_date)
    status_map = {r["status"]: r["count"] for r in by_status}
    delivered = status_map.get("delivered", 0)
    pending = sum(status_map.get(s, 0) for s in PENDING_ORDER_STATUSES)
    failed = status_map.get("failed", 0) + status_map.get("rejected", 0)
    return {
        "total": total,
        "delivered": delivered,
        "pending": pending,
        "failed": failed,
        "by_status": by_status,
    }


async def get_user_summary(session: AsyncSession, start_date=None, end_date=None) -> dict:  # noqa: ANN001,E501
    """User totals + new/active counts for the window."""
    total = await _count(session, User)
    new = await _count(session, User, *_range_clauses(User.created_at, start_date, end_date))
    active = await _count(session, User,
                          User.last_activity_at.is_not(None),
                          *_range_clauses(User.last_activity_at, start_date, end_date))
    blocked = await _count(session, User, User.is_blocked.is_(True))
    restricted = await _count(session, User, User.is_restricted.is_(True))
    with_wallet = await _count(session, User, User.wallet_balance > 0)
    p_start, p_end = get_previous_period(start_date, end_date)
    prev_new = await _count(session, User, *_range_clauses(User.created_at, p_start, p_end))
    return {
        "total": total,
        "new": new,
        "active": active,
        "blocked": blocked,
        "restricted": restricted,
        "with_wallet_balance": with_wallet,
        "new_change_pct": safe_percent_change(new, prev_new),
    }


async def get_product_summary(session: AsyncSession, start_date=None, end_date=None) -> dict:  # noqa: ANN001,E501
    """Catalog counts by type/visibility (window unused — catalog is current)."""
    total = await _count(session, Product)
    active = await _count(session, Product, Product.is_active.is_(True))
    hidden = await _count(session, Product, Product.is_hidden.is_(True))
    license_products = await _count(session, Product, Product.type == "license")
    v2ray_products = await _count(session, Product, Product.type == "v2ray")
    return {
        "total": total,
        "active": active,
        "hidden": hidden,
        "license": license_products,
        "v2ray": v2ray_products,
    }


async def get_dashboard_summary(session: AsyncSession, start_date=None, end_date=None) -> dict:  # noqa: ANN001,E501
    """One aggregate dict for the dashboard + reports overview page.

    Combines the revenue/order/user/product summaries with the operational
    "needs attention" counters (pending receipts, pending top-ups, low license
    stock, expiring/failed services, open tickets, marketing) — each queried
    with a single aggregate so the page stays fast.
    """
    revenue = await get_revenue_summary(session, start_date, end_date)
    orders = await get_order_summary(session, start_date, end_date)
    users = await get_user_summary(session, start_date, end_date)
    products = await get_product_summary(session, start_date, end_date)

    pending_receipts = await _count(session, Payment, Payment.status == "receipt_submitted")
    pending_topups = await _count(session, WalletTopupRequest,
                                  WalletTopupRequest.status == "waiting_admin")
    failed_orders = await _count(session, Order, Order.delivery_error.is_not(None),
                                 Order.status != "delivered")

    threshold = await SettingsService(session).get_int("license_low_stock_threshold", 5)
    low_stock = await get_low_stock_license_products(session, threshold)

    v2ray = await get_v2ray_service_summary(session)
    warn_days = await SettingsService(session).get_int("v2ray_expiry_warning_days", 3)
    expiring = await get_v2ray_expiring_soon(session, days=max(warn_days, 1))

    summary: dict[str, Any] = {
        "revenue": revenue,
        "orders": orders,
        "users": users,
        "products": products,
        "attention": {
            "pending_receipts": pending_receipts,
            "pending_topups": pending_topups,
            "failed_orders": failed_orders,
            "low_stock_products": len(low_stock),
            "v2ray_expiring_soon": len(expiring),
            "v2ray_failed": v2ray["by_status"].get("failed", 0),
        },
        "top_products": await get_top_products_by_revenue(session, start_date, end_date, limit=5),
        "v2ray": v2ray,
        "capabilities": {
            "coupons": COUPONS_AVAILABLE,
            "referrals": REFERRALS_AVAILABLE,
            "tickets": TICKETS_AVAILABLE,
        },
    }
    if TICKETS_AVAILABLE:
        summary["attention"]["open_tickets"] = (await get_open_ticket_summary(session)).get("open", 0)
    if COUPONS_AVAILABLE:
        summary["marketing_coupons"] = await get_coupon_usage_summary(session, start_date, end_date)
    if REFERRALS_AVAILABLE:
        summary["marketing_referrals"] = await get_referral_reward_summary(session, start_date, end_date)
    return summary


# ==========================================================================
# Sales
# ==========================================================================
async def get_sales_by_day(session: AsyncSession, start_date=None, end_date=None) -> list[dict]:  # noqa: ANN001,E501
    """Delivered-order revenue and count bucketed by delivery day."""
    day = func.date(Order.delivered_at)
    stmt = (
        select(day.label("day"),
               func.count().label("orders"),
               func.coalesce(func.sum(Order.final_amount), 0).label("revenue"))
        .where(Order.status.in_(REVENUE_ORDER_STATUSES), Order.delivered_at.is_not(None))
        .group_by(day).order_by(day)
    )
    stmt = _between(stmt, Order.delivered_at, start_date, end_date)
    rows = {}
    for r in (await session.execute(stmt)).all():
        key = _daystr(r.day)
        rows[key] = {"date": key, "orders": int(r.orders), "revenue": int(r.revenue)}
    return _fill_days(rows, start_date, end_date, {"orders": 0, "revenue": 0})


async def get_sales_by_product(session: AsyncSession, start_date=None, end_date=None, limit: int = 20) -> list[dict]:  # noqa: ANN001,E501
    day_clauses = _range_clauses(Order.delivered_at, start_date, end_date)
    stmt = (
        select(Product.id, Product.title, Product.type,
               func.count(Order.id).label("orders"),
               func.coalesce(func.sum(Order.final_amount), 0).label("revenue"))
        .join(Order, Order.product_id == Product.id)
        .where(Order.status.in_(REVENUE_ORDER_STATUSES), *day_clauses)
        .group_by(Product.id, Product.title, Product.type)
        .order_by(func.coalesce(func.sum(Order.final_amount), 0).desc())
        .limit(limit)
    )
    return [
        {"product_id": r.id, "title": r.title, "type": r.type,
         "orders": int(r.orders), "revenue": int(r.revenue)}
        for r in (await session.execute(stmt)).all()
    ]


async def get_sales_by_payment_method(session: AsyncSession, start_date=None, end_date=None) -> list[dict]:  # noqa: ANN001,E501
    day_clauses = _range_clauses(Order.delivered_at, start_date, end_date)
    stmt = (
        select(Order.payment_method,
               func.count().label("orders"),
               func.coalesce(func.sum(Order.final_amount), 0).label("revenue"))
        .where(Order.status.in_(REVENUE_ORDER_STATUSES), *day_clauses)
        .group_by(Order.payment_method).order_by(func.count().desc())
    )
    return [
        {"method": r.payment_method, "orders": int(r.orders), "revenue": int(r.revenue)}
        for r in (await session.execute(stmt)).all()
    ]


async def get_sales_by_product_type(session: AsyncSession, start_date=None, end_date=None) -> list[dict]:  # noqa: ANN001,E501
    day_clauses = _range_clauses(Order.delivered_at, start_date, end_date)
    stmt = (
        select(Product.type,
               func.count(Order.id).label("orders"),
               func.coalesce(func.sum(Order.final_amount), 0).label("revenue"))
        .join(Order, Order.product_id == Product.id)
        .where(Order.status.in_(REVENUE_ORDER_STATUSES), *day_clauses)
        .group_by(Product.type).order_by(func.count(Order.id).desc())
    )
    return [
        {"type": r.type, "orders": int(r.orders), "revenue": int(r.revenue)}
        for r in (await session.execute(stmt)).all()
    ]


# ==========================================================================
# Orders
# ==========================================================================
async def get_orders_by_status(session: AsyncSession, start_date=None, end_date=None) -> list[dict]:  # noqa: ANN001,E501
    rc = _range_clauses(Order.created_at, start_date, end_date)
    stmt = (
        select(Order.status, func.count().label("count"),
               func.coalesce(func.sum(Order.final_amount), 0).label("amount"))
        .where(*rc).group_by(Order.status).order_by(func.count().desc())
    )
    return [
        {"status": r.status, "count": int(r.count), "amount": int(r.amount)}
        for r in (await session.execute(stmt)).all()
    ]


async def get_orders_by_action_type(session: AsyncSession, start_date=None, end_date=None) -> list[dict]:  # noqa: ANN001,E501
    rc = _range_clauses(Order.created_at, start_date, end_date)
    action = func.coalesce(Order.action_type, "new_service")
    stmt = (
        select(action.label("action"), func.count().label("count"))
        .where(*rc).group_by(action).order_by(func.count().desc())
    )
    return [
        {"action_type": r.action, "count": int(r.count)}
        for r in (await session.execute(stmt)).all()
    ]


async def get_recent_orders(session: AsyncSession, limit: int = 20) -> list[dict]:
    stmt = select(Order).order_by(Order.id.desc()).limit(limit)
    rows = (await session.execute(stmt)).scalars().all()
    return [_order_brief(o) for o in rows]


async def get_failed_or_pending_orders(session: AsyncSession, limit: int = 20) -> list[dict]:
    stmt = (
        select(Order)
        .where((Order.delivery_error.is_not(None)) | (Order.status == "failed")
               | (Order.status.in_(PENDING_ORDER_STATUSES)))
        .order_by(Order.id.desc()).limit(limit)
    )
    rows = (await session.execute(stmt)).scalars().all()
    return [_order_brief(o) for o in rows]


def _order_brief(o: Order) -> dict:
    return {
        "id": o.id,
        "order_number": o.order_number,
        "user_id": o.user_id,
        "product": o.product.title if o.product else None,
        "status": o.status,
        "final_amount": int(o.final_amount or 0),
        "delivery_error": o.delivery_error,
        "created_at": o.created_at.isoformat() if o.created_at else None,
    }


# ==========================================================================
# Payments
# ==========================================================================
async def get_payments_by_status(session: AsyncSession, start_date=None, end_date=None) -> list[dict]:  # noqa: ANN001,E501
    rc = _range_clauses(Payment.created_at, start_date, end_date)
    stmt = (
        select(Payment.status, func.count().label("count"),
               func.coalesce(func.sum(Payment.amount), 0).label("amount"))
        .where(*rc).group_by(Payment.status).order_by(func.count().desc())
    )
    return [
        {"status": r.status, "count": int(r.count), "amount": int(r.amount)}
        for r in (await session.execute(stmt)).all()
    ]


async def get_payments_by_method(session: AsyncSession, start_date=None, end_date=None) -> list[dict]:  # noqa: ANN001,E501
    rc = _range_clauses(Payment.created_at, start_date, end_date)
    stmt = (
        select(Payment.method, func.count().label("count"),
               func.coalesce(func.sum(Payment.amount), 0).label("amount"))
        .where(Payment.status.in_(PAID_PAYMENT_STATUSES), *rc)
        .group_by(Payment.method).order_by(func.count().desc())
    )
    return [
        {"method": r.method, "count": int(r.count), "amount": int(r.amount)}
        for r in (await session.execute(stmt)).all()
    ]


async def get_pending_receipt_summary(session: AsyncSession) -> dict:
    count = await _count(session, Payment, Payment.status == "receipt_submitted")
    amount = await _sum(session, Payment.amount, Payment.status == "receipt_submitted")
    return {"count": count, "amount": amount}


async def get_wallet_topup_summary(session: AsyncSession, start_date=None, end_date=None) -> dict:  # noqa: ANN001,E501
    rc = _range_clauses(WalletTopupRequest.created_at, start_date, end_date)
    stmt = (
        select(WalletTopupRequest.status, func.count().label("count"),
               func.coalesce(func.sum(WalletTopupRequest.amount), 0).label("amount"))
        .where(*rc).group_by(WalletTopupRequest.status)
    )
    by_status = [
        {"status": r.status, "count": int(r.count), "amount": int(r.amount)}
        for r in (await session.execute(stmt)).all()
    ]
    approved = next((r["amount"] for r in by_status if r["status"] == "approved"), 0)
    pending = sum(r["count"] for r in by_status if r["status"] == "waiting_admin")
    return {"by_status": by_status, "approved_amount": approved, "pending_count": pending}


# ==========================================================================
# Wallet
# ==========================================================================
async def get_wallet_transaction_summary(session: AsyncSession, start_date=None, end_date=None) -> list[dict]:  # noqa: ANN001,E501
    rc = _range_clauses(WalletTransaction.created_at, start_date, end_date)
    stmt = (
        select(WalletTransaction.type, func.count().label("count"),
               func.coalesce(func.sum(WalletTransaction.amount), 0).label("amount"))
        .where(WalletTransaction.status == "completed", *rc)
        .group_by(WalletTransaction.type).order_by(func.count().desc())
    )
    return [
        {"type": r.type, "count": int(r.count), "amount": int(r.amount)}
        for r in (await session.execute(stmt)).all()
    ]


async def get_wallet_balance_changes(session: AsyncSession, start_date=None, end_date=None) -> dict:  # noqa: ANN001,E501
    rc = _range_clauses(WalletTransaction.created_at, start_date, end_date)
    credits = await _sum(session, WalletTransaction.amount,
                         WalletTransaction.status == "completed",
                         WalletTransaction.amount > 0, *rc)
    debits = await _sum(session, WalletTransaction.amount,
                        WalletTransaction.status == "completed",
                        WalletTransaction.amount < 0, *rc)
    count = await _count(session, WalletTransaction, WalletTransaction.status == "completed", *rc)
    return {"credits": credits, "debits": debits, "net": credits + debits, "count": count}


async def get_top_wallet_users(session: AsyncSession, limit: int = 20) -> list[dict]:
    stmt = (
        select(User.id, User.telegram_id, User.username, User.wallet_balance)
        .where(User.wallet_balance > 0)
        .order_by(User.wallet_balance.desc()).limit(limit)
    )
    return [
        {"user_id": r.id, "telegram_id": r.telegram_id, "username": r.username,
         "wallet_balance": int(r.wallet_balance)}
        for r in (await session.execute(stmt)).all()
    ]


# ==========================================================================
# Products
# ==========================================================================
async def get_top_products_by_revenue(session: AsyncSession, start_date=None, end_date=None, limit: int = 20) -> list[dict]:  # noqa: ANN001,E501
    return await get_sales_by_product(session, start_date, end_date, limit=limit)


async def get_top_products_by_orders(session: AsyncSession, start_date=None, end_date=None, limit: int = 20) -> list[dict]:  # noqa: ANN001,E501
    day_clauses = _range_clauses(Order.delivered_at, start_date, end_date)
    stmt = (
        select(Product.id, Product.title, Product.type,
               func.count(Order.id).label("orders"),
               func.coalesce(func.sum(Order.final_amount), 0).label("revenue"))
        .join(Order, Order.product_id == Product.id)
        .where(Order.status.in_(REVENUE_ORDER_STATUSES), *day_clauses)
        .group_by(Product.id, Product.title, Product.type)
        .order_by(func.count(Order.id).desc()).limit(limit)
    )
    return [
        {"product_id": r.id, "title": r.title, "type": r.type,
         "orders": int(r.orders), "revenue": int(r.revenue)}
        for r in (await session.execute(stmt)).all()
    ]


async def get_inactive_products(session: AsyncSession) -> list[dict]:
    stmt = (
        select(Product).where((Product.is_active.is_(False)) | (Product.is_hidden.is_(True)))
        .order_by(Product.id.desc())
    )
    rows = (await session.execute(stmt)).scalars().all()
    return [
        {"product_id": p.id, "title": p.title, "type": p.type,
         "is_active": p.is_active, "is_hidden": p.is_hidden, "price": int(p.price or 0)}
        for p in rows
    ]


async def get_low_stock_license_products(session: AsyncSession, threshold: int | None = None) -> list[dict]:  # noqa: ANN001,E501
    """License products whose available stock is below the threshold."""
    if threshold is None:
        threshold = await SettingsService(session).get_int("license_low_stock_threshold", 5)
    avail = func.sum(
        case((LicenseItem.status == "available", 1), else_=0)
    )
    stmt = (
        select(Product.id, Product.title, avail.label("available"))
        .join(LicenseItem, LicenseItem.product_id == Product.id, isouter=True)
        .where(Product.type == "license")
        .group_by(Product.id, Product.title)
        .having(func.coalesce(avail, 0) < threshold)
        .order_by(func.coalesce(avail, 0).asc())
    )
    return [
        {"product_id": r.id, "title": r.title, "available": int(r.available or 0),
         "threshold": threshold}
        for r in (await session.execute(stmt)).all()
    ]


# ==========================================================================
# Users
# ==========================================================================
async def get_user_growth_by_day(session: AsyncSession, start_date=None, end_date=None) -> list[dict]:  # noqa: ANN001,E501
    day = func.date(User.created_at)
    stmt = select(day.label("day"), func.count().label("count")).group_by(day).order_by(day)
    stmt = _between(stmt, User.created_at, start_date, end_date)
    rows = {}
    running = 0
    ordered = [(_daystr(r.day), int(r.count)) for r in (await session.execute(stmt)).all()]
    for key, count in ordered:
        rows[key] = {"date": key, "count": count}
    series = _fill_days(rows, start_date, end_date, {"count": 0})
    # add a cumulative column for growth charts
    for item in series:
        running += item["count"]
        item["cumulative"] = running
    return series


async def get_new_users(session: AsyncSession, start_date=None, end_date=None) -> int:
    return await _count(session, User, *_range_clauses(User.created_at, start_date, end_date))


async def get_active_users(session: AsyncSession, start_date=None, end_date=None) -> int:
    return await _count(session, User, User.last_activity_at.is_not(None),
                        *_range_clauses(User.last_activity_at, start_date, end_date))


async def get_blocked_restricted_users(session: AsyncSession) -> dict:
    blocked = await _count(session, User, User.is_blocked.is_(True))
    restricted = await _count(session, User, User.is_restricted.is_(True))
    return {"blocked": blocked, "restricted": restricted}


# ==========================================================================
# License stock / sales
# ==========================================================================
async def get_license_stock_summary(session: AsyncSession) -> dict:
    stmt = (
        select(LicenseItem.status, func.count().label("count"))
        .group_by(LicenseItem.status)
    )
    by_status = {r.status: int(r.count) for r in (await session.execute(stmt)).all()}
    return {
        "by_status": by_status,
        "available": by_status.get("available", 0),
        "reserved": by_status.get("reserved", 0),
        "sold": by_status.get("sold", 0),
        "total": sum(by_status.values()),
    }


async def get_license_sales_summary(session: AsyncSession, start_date=None, end_date=None) -> dict:  # noqa: ANN001,E501
    rc = _range_clauses(LicenseItem.sold_at, start_date, end_date)
    sold = await _count(session, LicenseItem, LicenseItem.status == "sold", *rc)
    day = func.date(LicenseItem.sold_at)
    stmt = (
        select(day.label("day"), func.count().label("count"))
        .where(LicenseItem.status == "sold", LicenseItem.sold_at.is_not(None))
        .group_by(day).order_by(day)
    )
    stmt = _between(stmt, LicenseItem.sold_at, start_date, end_date)
    rows = {}
    for r in (await session.execute(stmt)).all():
        key = _daystr(r.day)
        rows[key] = {"date": key, "count": int(r.count)}
    return {"sold": sold, "by_day": _fill_days(rows, start_date, end_date, {"count": 0})}


async def get_low_stock_summary(session: AsyncSession, threshold: int | None = None) -> list[dict]:
    return await get_low_stock_license_products(session, threshold)


# ==========================================================================
# V2Ray services
# ==========================================================================
async def get_v2ray_service_summary(session: AsyncSession) -> dict:
    stmt = select(V2RayService.status, func.count().label("count")).group_by(V2RayService.status)
    by_status = {r.status: int(r.count) for r in (await session.execute(stmt)).all()}
    total_traffic = await _sum(session, V2RayService.used_gb)
    return {
        "by_status": by_status,
        "active": by_status.get("active", 0),
        "total": sum(by_status.values()),
        "used_bytes": total_traffic,
    }


async def get_v2ray_expiring_soon(session: AsyncSession, days: int = 7) -> list[dict]:
    now = datetime.now(UTC)
    horizon = now + timedelta(days=max(days, 0))
    stmt = (
        select(V2RayService)
        .where(V2RayService.status == "active",
               V2RayService.expire_at.is_not(None),
               V2RayService.expire_at >= now,
               V2RayService.expire_at <= horizon)
        .order_by(V2RayService.expire_at.asc()).limit(200)
    )
    rows = (await session.execute(stmt)).scalars().all()
    return [_v2ray_brief(s) for s in rows]


async def get_v2ray_over_quota_or_failed(session: AsyncSession, limit: int = 200) -> list[dict]:
    stmt = (
        select(V2RayService)
        .where(V2RayService.status.in_(("over_quota", "failed")))
        .order_by(V2RayService.id.desc()).limit(limit)
    )
    rows = (await session.execute(stmt)).scalars().all()
    return [_v2ray_brief(s) for s in rows]


async def get_v2ray_usage_summary(session: AsyncSession, limit: int = 20) -> list[dict]:
    """Top services by used traffic (for the usage chart)."""
    stmt = (
        select(V2RayService.id, V2RayService.client_email, V2RayService.user_id,
               V2RayService.used_gb, V2RayService.total_gb, V2RayService.status)
        .order_by(V2RayService.used_gb.desc()).limit(limit)
    )
    return [
        {"id": r.id, "label": _mask_email(r.client_email), "user_id": r.user_id,
         "used_bytes": int(r.used_gb or 0), "total_bytes": int(r.total_gb or 0),
         "status": r.status}
        for r in (await session.execute(stmt)).all()
    ]


def _mask_email(email: str | None) -> str:
    if not email:
        return ""
    local, _, domain = email.partition("@")
    head = local[:3]
    return f"{head}***@{domain}" if domain else f"{head}***"


def _v2ray_brief(s: V2RayService) -> dict:
    return {
        "id": s.id,
        "user_id": s.user_id,
        "label": _mask_email(s.client_email),
        "status": s.status,
        "used_bytes": int(s.used_gb or 0),
        "total_bytes": int(s.total_gb or 0),
        "expire_at": s.expire_at.isoformat() if s.expire_at else None,
    }


# ==========================================================================
# Coupons / referrals (optional — Phase 10)
# ==========================================================================
async def get_coupon_usage_summary(session: AsyncSession, start_date=None, end_date=None) -> dict:  # noqa: ANN001,E501
    if not COUPONS_AVAILABLE:
        return {"available": False}
    rc = _range_clauses(CouponUsage.created_at, start_date, end_date)
    count = await _count(session, CouponUsage, *rc)
    discount = await _sum(session, CouponUsage.discount_amount, *rc)
    stmt = (
        select(Coupon.code, func.count(CouponUsage.id).label("uses"),
               func.coalesce(func.sum(CouponUsage.discount_amount), 0).label("discount"))
        .join(CouponUsage, CouponUsage.coupon_id == Coupon.id)
        .where(*rc)
        .group_by(Coupon.code).order_by(func.count(CouponUsage.id).desc()).limit(20)
    )
    top = [
        {"code": r.code, "uses": int(r.uses), "discount": int(r.discount)}
        for r in (await session.execute(stmt)).all()
    ]
    return {"available": True, "uses": count, "total_discount": discount, "top": top}


async def get_referral_reward_summary(session: AsyncSession, start_date=None, end_date=None) -> dict:  # noqa: ANN001,E501
    if not REFERRALS_AVAILABLE:
        return {"available": False}
    rc = _range_clauses(ReferralReward.created_at, start_date, end_date)
    stmt = (
        select(ReferralReward.status, func.count().label("count"),
               func.coalesce(func.sum(ReferralReward.reward_amount), 0).label("amount"))
        .where(*rc).group_by(ReferralReward.status)
    )
    by_status = [
        {"status": r.status, "count": int(r.count), "amount": int(r.amount)}
        for r in (await session.execute(stmt)).all()
    ]
    paid = next((r["amount"] for r in by_status if r["status"] == "paid"), 0)
    pending = sum(r["count"] for r in by_status if r["status"] == "pending")
    return {"available": True, "by_status": by_status, "paid_amount": paid,
            "pending_count": pending}


# ==========================================================================
# Tickets (optional — Phase 9)
# ==========================================================================
async def get_ticket_summary(session: AsyncSession, start_date=None, end_date=None) -> dict:  # noqa: ANN001,E501
    if not TICKETS_AVAILABLE:
        return {"available": False}
    rc = _range_clauses(Ticket.created_at, start_date, end_date)
    stmt = (
        select(Ticket.status, func.count().label("count"))
        .where(*rc).group_by(Ticket.status)
    )
    by_status = {r.status: int(r.count) for r in (await session.execute(stmt)).all()}
    return {"available": True, "by_status": by_status, "total": sum(by_status.values())}


async def get_open_ticket_summary(session: AsyncSession) -> dict:
    if not TICKETS_AVAILABLE:
        return {"available": False, "open": 0}
    open_count = await _count(session, Ticket, Ticket.status.in_(OPEN_TICKET_STATUSES))
    stmt = (
        select(Ticket.priority, func.count().label("count"))
        .where(Ticket.status.in_(OPEN_TICKET_STATUSES))
        .group_by(Ticket.priority)
    )
    by_priority = {r.priority: int(r.count) for r in (await session.execute(stmt)).all()}
    return {"available": True, "open": open_count, "by_priority": by_priority}

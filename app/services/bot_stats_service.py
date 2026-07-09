"""Telegram-admin bot statistics (the "📊 آمار ربات" panel).

Read-only aggregates for the in-bot admin stats section. Every figure comes from
an efficient SQL ``COUNT``/``SUM``/``GROUP BY`` — rows are never loaded into
memory. All timestamps are UTC; ranges are half-open ``[start, end)``.

Definitions
-----------
* A **sale** is a *delivered* order (``Order.status == "delivered"``), matching
  ``report_service``'s revenue recognition, ranged on ``delivered_at``.
* A **successful payment** (gateway stats) is ``Payment.status == "approved"``,
  ranged on ``approved_at``.
* An **active-service** sale is a delivered order that still has a linked
  ``V2RayService`` in status ``active``. License / Apple-ID products have no
  "active" lifecycle, so they never count toward active-service figures.

Not modelled yet (documented, return 0): **test accounts** and **resellers**
(no such tables exist), so ``total_test_accounts``/``total_resellers``/
``n_resellers_count``/``n2_resellers_count`` are always 0 until those features
land.
"""
from __future__ import annotations

import calendar
import logging
import re
from datetime import datetime, timedelta, timezone

from sqlalchemy import func, or_, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.models.order import Order
from app.models.payment import Payment
from app.models.product import Product
from app.models.user import User
from app.models.v2ray_service import V2RayService
from app.models.xui_server import XuiServer
from app.services import payment_core_service

log = logging.getLogger("bot_stats")

SALE_STATUSES: tuple[str, ...] = ("delivered",)
PAID_PAYMENT_STATUSES: tuple[str, ...] = ("approved",)
ACTIVE_SERVICE_STATUS = "active"

# The valid range presets the bot exposes.
RANGE_TYPES: tuple[str, ...] = (
    "all", "last_hour", "today", "yesterday", "current_month", "previous_month", "custom",
)

# Persian + Arabic-Indic digits -> ASCII, so a user can type "۱۴۰۴/۰۱/۰۱".
_DIGIT_MAP = {ord(p): str(i) for i, p in enumerate("۰۱۲۳۴۵۶۷۸۹")}
_DIGIT_MAP.update({ord(a): str(i) for i, a in enumerate("٠١٢٣٤٥٦٧٨٩")})


def _now() -> datetime:
    return datetime.now(timezone.utc)


async def _scalar(session: AsyncSession, stmt) -> int:
    """Run a scalar aggregate and coerce NULL/None to 0."""
    return int(await session.scalar(stmt) or 0)


# ---------------------------------------------------------------------------
# Date ranges
# ---------------------------------------------------------------------------
def calculate_range(
    range_type: str = "all", *, start_at: datetime | None = None, end_at: datetime | None = None,
) -> tuple[datetime | None, datetime | None]:
    """Half-open ``[start, end)`` UTC window for a preset range.

    ``all`` → (None, None) (no bound). ``custom`` returns the given
    ``start_at``/``end_at`` verbatim. The calendar presets reuse
    ``report_service.parse_date_range`` so the whole app agrees on boundaries.
    """
    from app.services import report_service

    rt = (range_type or "all").lower()
    if rt == "all":
        return None, None
    if rt == "last_hour":
        now = _now()
        return now - timedelta(hours=1), now
    if rt == "today":
        return report_service.parse_date_range(preset="today")
    if rt == "yesterday":
        return report_service.parse_date_range(preset="yesterday")
    if rt == "current_month":
        return report_service.parse_date_range(preset="this_month")
    if rt == "previous_month":
        return report_service.parse_date_range(preset="last_month")
    if rt == "custom":
        return start_at, end_at
    return None, None


def jalali_to_gregorian(jy: int, jm: int, jd: int) -> tuple[int, int, int]:
    """Convert a Jalali (Shamsi) date to Gregorian ``(year, month, day)``.

    Dependency-free implementation of the standard algorithm so the project does
    not have to pull in ``jdatetime`` just for the stats date picker.
    """
    jy += 1595
    days = -355668 + (365 * jy) + ((jy // 33) * 8) + (((jy % 33) + 3) // 4) + jd
    if jm < 7:
        days += (jm - 1) * 31
    else:
        days += ((jm - 7) * 30) + 186
    gy = 400 * (days // 146097)
    days %= 146097
    if days > 36524:
        days -= 1
        gy += 100 * (days // 36524)
        days %= 36524
        if days >= 365:
            days += 1
    gy += 4 * (days // 1461)
    days %= 1461
    if days > 365:
        gy += (days - 1) // 365
        days = (days - 1) % 365
    gd = days + 1
    leap = (gy % 4 == 0 and gy % 100 != 0) or (gy % 400 == 0)
    sal_a = [0, 31, 29 if leap else 28, 31, 30, 31, 30, 31, 31, 30, 31, 30, 31]
    gm = 0
    while gm < 13 and gd > sal_a[gm]:
        gd -= sal_a[gm]
        gm += 1
    return gy, gm, gd


def parse_jalali_date(date_text: str, *, end_of_day: bool = False) -> datetime | None:
    """Parse ``YYYY/MM/DD`` into a UTC midnight datetime, or None if invalid.

    Jalali is assumed (owner examples like ``1404/01/01``); a 4-digit year ≥ 1500
    is treated as already-Gregorian so both notations work. Persian/Arabic digits
    and ``/ - .`` separators are accepted. ``end_of_day`` returns the *next*
    midnight so the day is inclusive in a half-open ``col < end`` range.
    """
    if not date_text:
        return None
    normalized = date_text.strip().translate(_DIGIT_MAP)
    parts = [p for p in re.split(r"[/\-.]", normalized) if p != ""]
    if len(parts) != 3:
        return None
    try:
        y, m, d = int(parts[0]), int(parts[1]), int(parts[2])
    except ValueError:
        return None
    if y >= 1500:
        gy, gm, gd = y, m, d
    else:
        # Validate the Jalali fields before converting — the algorithm silently
        # overflows an impossible date (e.g. month 13) into a valid Gregorian one.
        if not (1 <= m <= 12):
            return None
        max_day = 31 if m <= 6 else 30  # months 1-6 → 31; 7-11 → 30; 12 → up to 30 (leap)
        if not (1 <= d <= max_day):
            return None
        gy, gm, gd = jalali_to_gregorian(y, m, d)
    try:
        dt = datetime(gy, gm, gd, tzinfo=timezone.utc)
    except ValueError:
        return None
    return dt + timedelta(days=1) if end_of_day else dt


# ---------------------------------------------------------------------------
# Aggregates
# ---------------------------------------------------------------------------
def _sale_clauses(start: datetime | None, end: datetime | None) -> list:
    clauses = [Order.status.in_(SALE_STATUSES)]
    if start is not None:
        clauses.append(Order.delivered_at >= start)
    if end is not None:
        clauses.append(Order.delivered_at < end)
    return clauses


async def _predict_monthly_income(
    session: AsyncSession, range_type: str, start: datetime | None, end: datetime | None,
    total_sales_amount: int,
) -> int:
    """Best-effort monthly income projection (documented, never negative).

    * ``all`` / ``current_month``: take THIS month's sales-to-date and scale to a
      full month — ``month_sales / day_of_month * days_in_month``.
    * any dated window: daily average over the window × 30 —
      ``total_sales_amount / days_in_window * 30``.
    * no data → 0.
    """
    rt = (range_type or "all").lower()
    if rt in ("all", "current_month"):
        now = _now()
        month_start = now.replace(day=1, hour=0, minute=0, second=0, microsecond=0)
        days_in_month = calendar.monthrange(now.year, now.month)[1]
        month_sales = await _scalar(
            session,
            select(func.coalesce(func.sum(Order.final_amount), 0)).where(
                Order.status.in_(SALE_STATUSES), Order.delivered_at >= month_start),
        )
        if now.day <= 0 or month_sales <= 0:
            return 0
        return int(month_sales / now.day * days_in_month)
    if start is not None and end is not None and total_sales_amount > 0:
        days = max(1, (end - start).days)
        return int(total_sales_amount / days * 30)
    return 0


async def get_bot_stats(
    session: AsyncSession, range_type: str = "all",
    start_at: datetime | None = None, end_at: datetime | None = None,
) -> dict:
    """All top-level bot statistics for a range (see module docstring)."""
    start, end = calculate_range(range_type, start_at=start_at, end_at=end_at)
    sale = _sale_clauses(start, end)

    total_users = await _scalar(session, select(func.count(User.id)))
    total_users_balance = await _scalar(
        session, select(func.coalesce(func.sum(User.wallet_balance), 0)))
    users_with_purchase = await _scalar(
        session, select(func.count(func.distinct(Order.user_id))).where(*sale))

    total_sales_count = await _scalar(session, select(func.count(Order.id)).where(*sale))
    total_sales_amount = await _scalar(
        session, select(func.coalesce(func.sum(Order.final_amount), 0)).where(*sale))

    # Active-service sales: delivered orders still backed by an active V2Ray client.
    active_count = await _scalar(
        session,
        select(func.count(Order.id)).select_from(Order)
        .join(V2RayService, V2RayService.order_id == Order.id)
        .where(*sale, V2RayService.status == ACTIVE_SERVICE_STATUS),
    )
    active_amount = await _scalar(
        session,
        select(func.coalesce(func.sum(Order.final_amount), 0)).select_from(Order)
        .join(V2RayService, V2RayService.order_id == Order.id)
        .where(*sale, V2RayService.status == ACTIVE_SERVICE_STATUS),
    )

    total_renew_amount = await _scalar(
        session,
        select(func.coalesce(func.sum(Order.final_amount), 0))
        .where(*sale, Order.action_type == "renew_service"),
    )
    total_discount_amount = await _scalar(
        session, select(func.coalesce(func.sum(Order.discount_amount), 0)).where(*sale))
    discount_usage_count = await _scalar(
        session, select(func.count(Order.id)).where(*sale, Order.coupon_id.is_not(None)))

    total_panels = await _scalar(session, select(func.count(XuiServer.id)))

    conversion_rate = round(users_with_purchase / total_users * 100, 2) if total_users else 0.0
    average_purchase = int(total_sales_amount / users_with_purchase) if users_with_purchase else 0
    renew_percent = (round(total_renew_amount / total_sales_amount * 100, 2)
                     if total_sales_amount else 0.0)
    predicted = await _predict_monthly_income(
        session, range_type, start, end, total_sales_amount)

    return {
        "range_type": (range_type or "all").lower(),
        "total_users": total_users,
        "users_with_purchase": users_with_purchase,
        "total_test_accounts": 0,          # no test-account model yet
        "total_users_balance": total_users_balance,
        "total_sales_count": total_sales_count,
        "active_services_sales_count": active_count,
        "total_sales_amount": total_sales_amount,
        "active_services_sales_amount": active_amount,
        "total_renew_amount": total_renew_amount,
        "total_discount_amount": total_discount_amount,
        "discount_usage_count": discount_usage_count,
        "conversion_rate": conversion_rate,
        "average_purchase_per_customer": average_purchase,
        "predicted_monthly_income": predicted,
        "renew_percent_from_sales": renew_percent,
        "total_resellers": 0,              # no reseller model yet
        "n_resellers_count": 0,            # no reseller model yet
        "n2_resellers_count": 0,           # no reseller model yet
        "total_panels": total_panels,
    }


async def get_gateway_stats(
    session: AsyncSession, start_at: datetime | None = None, end_at: datetime | None = None,
) -> list[dict]:
    """Successful-payment stats per ACTIVE payment method (inactive ones excluded).

    A payment belongs to a method when its ``provider_name`` equals the method
    ``code`` (new invoice flow) or its legacy ``method`` string matches the
    method type. Active methods with no payments show 0 rather than being hidden.
    """
    methods = [m for m in await payment_core_service.list_methods(session) if m.is_active]
    out: list[dict] = []
    for m in methods:
        legacy = "card_to_card" if m.method_type == "manual_receipt" else m.method_type
        match = or_(Payment.provider_name == m.code, Payment.method == legacy)
        clauses = [Payment.status.in_(PAID_PAYMENT_STATUSES), match]
        if start_at is not None:
            clauses.append(Payment.approved_at >= start_at)
        if end_at is not None:
            clauses.append(Payment.approved_at < end_at)
        count = await _scalar(session, select(func.count(Payment.id)).where(*clauses))
        amount = await _scalar(
            session, select(func.coalesce(func.sum(Payment.amount), 0)).where(*clauses))
        out.append({
            "gateway_name": m.title,
            "code": m.code,
            "successful_payments_count": count,
            "successful_payments_amount": amount,
        })
    return out


async def get_product_sales_comparison(
    session: AsyncSession, start_at: datetime | None = None, end_at: datetime | None = None,
) -> list[dict]:
    """Per-product delivered-sales count + amount for a window, richest first.

    Uses an inner join + GROUP BY so only products with at least one sale in the
    window appear (an empty window yields an empty list).
    """
    clauses = [Order.status.in_(SALE_STATUSES)]
    if start_at is not None:
        clauses.append(Order.delivered_at >= start_at)
    if end_at is not None:
        clauses.append(Order.delivered_at < end_at)
    stmt = (
        select(
            Product.id, Product.title,
            func.count(Order.id), func.coalesce(func.sum(Order.final_amount), 0),
        )
        .select_from(Order).join(Product, Product.id == Order.product_id)
        .where(*clauses)
        .group_by(Product.id, Product.title)
        .order_by(func.coalesce(func.sum(Order.final_amount), 0).desc())
    )
    rows = (await session.execute(stmt)).all()
    return [
        {"product_id": r[0], "product_name": r[1],
         "sales_count": int(r[2] or 0), "sales_amount": int(r[3] or 0)}
        for r in rows
    ]

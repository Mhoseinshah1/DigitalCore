"""Discount coupons (Phase 10): validation, application, and race-safe consumption.

Codes are normalized (UPPERCASE, trimmed) everywhere. `validate_coupon` returns a
precise error code for every rejection so the bot/web can show a translated
message. A coupon is *applied* to a still-pending order (recomputing
`final_amount`) and only *consumed* — `used_count` bumped + a `CouponUsage` row
written — when the order is actually paid, under a coupon row lock and guarded by
the ``(coupon_id, order_id)`` unique constraint so a retry never double-counts.
Money is integer toman; a coupon can never push `final_amount` below zero.
"""
from __future__ import annotations

import logging
from datetime import datetime, timezone

from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.settings_service import SettingsService
from app.models.coupon import (
    COUPON_ACTIONS,
    COUPON_DISCOUNT_TYPES,
    COUPON_PRODUCT_TYPES,
    Coupon,
)
from app.models.coupon_usage import CouponUsage
from app.models.product import Product
from app.services import audit_service, order_service

log = logging.getLogger("coupon")


class CouponError(ValueError):
    """A user-facing reason a coupon was rejected (carries a stable `.code`)."""

    code = "coupon_error"

    def __init__(self, message: str, code: str | None = None) -> None:
        super().__init__(message)
        if code is not None:
            self.code = code


def _now() -> datetime:
    return datetime.now(timezone.utc)


def _aware(dt: datetime | None) -> datetime | None:
    """Treat a naive datetime (read back from SQLite) as UTC."""
    if dt is None or dt.tzinfo is not None:
        return dt
    return dt.replace(tzinfo=timezone.utc)


def normalize_code(code: str | None) -> str:
    return (code or "").strip().upper()


async def coupons_enabled(session: AsyncSession) -> bool:
    return await SettingsService(session).get_bool("coupons_enabled", True)


# --------------------------------------------------------------------------
# Discount math (pure)
# --------------------------------------------------------------------------
def calculate_discount(coupon: Coupon, amount: int) -> int:
    """Discount (toman) for `amount`. Never negative, never exceeds `amount`, and
    percent discounts are capped by `max_discount_amount`."""
    amount = int(amount or 0)
    if amount <= 0:
        return 0
    if coupon.discount_type == "percent":
        discount = amount * int(coupon.discount_value or 0) // 100
        if coupon.max_discount_amount:
            discount = min(discount, int(coupon.max_discount_amount))
    else:  # fixed
        discount = int(coupon.discount_value or 0)
    return max(0, min(discount, amount))


# --------------------------------------------------------------------------
# Queries
# --------------------------------------------------------------------------
async def get_coupon(session: AsyncSession, coupon_id: int) -> Coupon | None:
    return await session.get(Coupon, coupon_id)


async def get_by_code(session: AsyncSession, code: str) -> Coupon | None:
    return await session.scalar(select(Coupon).where(Coupon.code == normalize_code(code)))


async def list_coupons(
    session: AsyncSession, *, active_only: bool = False, limit: int = 200, offset: int = 0
) -> list[Coupon]:
    stmt = select(Coupon)
    if active_only:
        stmt = stmt.where(Coupon.is_active.is_(True))
    stmt = stmt.order_by(Coupon.id.desc()).limit(limit).offset(offset)
    return list((await session.execute(stmt)).scalars().all())


async def list_public_coupons(session: AsyncSession, *, limit: int = 20) -> list[Coupon]:
    """Active, currently-valid, unrestricted coupons for the /coupons page."""
    now = _now()
    rows = await list_coupons(session, active_only=True, limit=200)
    out = []
    for c in rows:
        if c.starts_at and _aware(c.starts_at) > now:
            continue
        if c.expires_at and _aware(c.expires_at) < now:
            continue
        if c.usage_limit is not None and int(c.used_count or 0) >= c.usage_limit:
            continue
        out.append(c)
        if len(out) >= limit:
            break
    return out


async def list_coupon_usages(
    session: AsyncSession, coupon_id: int, *, limit: int = 200
) -> list[CouponUsage]:
    stmt = (select(CouponUsage).where(CouponUsage.coupon_id == coupon_id)
            .order_by(CouponUsage.id.desc()).limit(limit))
    return list((await session.execute(stmt)).scalars().all())


async def _user_usage_count(session: AsyncSession, coupon_id: int, user_id: int) -> int:
    return int(await session.scalar(
        select(func.count(CouponUsage.id)).where(
            CouponUsage.coupon_id == coupon_id, CouponUsage.user_id == user_id)
    ) or 0)


# --------------------------------------------------------------------------
# Validation
# --------------------------------------------------------------------------
async def validate_coupon(
    session: AsyncSession, code: str, user_id: int, product_id: int | None,
    order_amount: int, *, action_type: str | None = None,
) -> tuple[Coupon, int]:
    """Validate a coupon for a would-be order. Returns (coupon, discount) or raises
    CouponError with a precise code."""
    if not await coupons_enabled(session):
        raise CouponError("coupons are disabled", code="coupons_disabled")
    coupon = await get_by_code(session, code)
    if coupon is None:
        raise CouponError("coupon not found", code="coupon_not_found")
    if not coupon.is_active:
        raise CouponError("coupon is inactive", code="coupon_inactive")

    now = _now()
    if coupon.starts_at and _aware(coupon.starts_at) > now:
        raise CouponError("coupon has not started", code="coupon_not_started")
    if coupon.expires_at and _aware(coupon.expires_at) < now:
        raise CouponError("coupon has expired", code="coupon_expired")

    if coupon.usage_limit is not None and int(coupon.used_count or 0) >= coupon.usage_limit:
        raise CouponError("coupon usage limit reached", code="usage_limit_reached")
    if coupon.usage_limit_per_user is not None:
        if await _user_usage_count(session, coupon.id, user_id) >= coupon.usage_limit_per_user:
            raise CouponError("you have already used this coupon",
                              code="user_usage_limit_reached")

    if coupon.min_order_amount and int(order_amount) < int(coupon.min_order_amount):
        raise CouponError("order amount is below the minimum", code="min_order_amount_not_met")

    if coupon.product_id is not None and coupon.product_id != product_id:
        raise CouponError("coupon not valid for this product", code="product_not_allowed")

    if coupon.product_type and coupon.product_type != "any":
        product = await session.get(Product, product_id) if product_id else None
        if product is None or product.type != coupon.product_type:
            raise CouponError("coupon not valid for this product type",
                              code="product_type_not_allowed")

    if coupon.applies_to_action and coupon.applies_to_action != "any":
        action = action_type or "new_purchase"
        if coupon.applies_to_action != action:
            raise CouponError("coupon not valid for this action", code="action_not_allowed")

    return coupon, calculate_discount(coupon, order_amount)


# --------------------------------------------------------------------------
# Apply / remove on an order
# --------------------------------------------------------------------------
def _order_action(order) -> str:
    return order.action_type or "new_purchase"


async def apply_coupon_to_order(session: AsyncSession, order_id: int, code: str, user_id: int):
    """Attach a validated coupon to a still-pending order and recompute final_amount."""
    order = await order_service.get_order(session, order_id)
    if order is None:
        raise CouponError("order not found", code="order_not_found")
    if order.user_id != user_id:
        raise CouponError("not your order", code="not_your_order")
    if order.status != "pending_payment":
        raise CouponError("order can no longer be changed", code="order_locked")

    coupon, discount = await validate_coupon(
        session, code, user_id, order.product_id, int(order.amount or 0),
        action_type=_order_action(order))
    order.coupon_id = coupon.id
    order.coupon_code = coupon.code
    order.discount_amount = discount
    order.final_amount = max(0, int(order.amount or 0) - discount)
    await audit_service.log(
        session, actor_type="user", actor_id=user_id, action="coupon_applied",
        target_type="order", target_id=order.id,
        meta=f"code={coupon.code} discount={discount} final={order.final_amount}",
    )
    await session.refresh(order)
    return order


async def remove_coupon_from_order(session: AsyncSession, order_id: int, user_id: int):
    """Detach a coupon from a still-pending order and restore final_amount."""
    order = await order_service.get_order(session, order_id)
    if order is None:
        raise CouponError("order not found", code="order_not_found")
    if order.user_id != user_id:
        raise CouponError("not your order", code="not_your_order")
    if order.status != "pending_payment":
        raise CouponError("order can no longer be changed", code="order_locked")
    if not order.coupon_id:
        return order
    prev = order.coupon_code
    order.coupon_id = None
    order.coupon_code = None
    order.discount_amount = 0
    order.final_amount = int(order.amount or 0)
    await audit_service.log(
        session, actor_type="user", actor_id=user_id, action="coupon_removed",
        target_type="order", target_id=order.id, meta=f"code={prev}",
    )
    await session.refresh(order)
    return order


async def record_usage(session: AsyncSession, order_id: int) -> bool:
    """Consume the order's coupon (paid order). Race-safe + idempotent.

    Locks the coupon row, writes a CouponUsage (unique per order), and bumps
    used_count. A second call for the same order is a no-op. Returns True when a
    usage was newly recorded."""
    order = await order_service.get_order(session, order_id)
    if order is None or not order.coupon_id:
        return False
    coupon = await session.scalar(
        select(Coupon).where(Coupon.id == order.coupon_id).with_for_update()
    )
    if coupon is None:
        return False
    existing = await session.scalar(
        select(CouponUsage.id).where(
            CouponUsage.coupon_id == coupon.id, CouponUsage.order_id == order.id)
    )
    if existing is not None:
        return False  # already consumed for this order
    session.add(CouponUsage(coupon_id=coupon.id, user_id=order.user_id,
                            order_id=order.id, discount_amount=int(order.discount_amount or 0)))
    coupon.used_count = int(coupon.used_count or 0) + 1
    await audit_service.log(
        session, actor_type="user", actor_id=order.user_id, action="coupon_consumed",
        target_type="coupon", target_id=coupon.id,
        meta=f"order={order.order_number} discount={order.discount_amount}",
    )
    return True


# --------------------------------------------------------------------------
# Admin CRUD
# --------------------------------------------------------------------------
def _validate_fields(discount_type: str, discount_value: int, product_type: str | None,
                     applies_to_action: str | None) -> None:
    if discount_type not in COUPON_DISCOUNT_TYPES:
        raise CouponError(f"discount_type must be one of {COUPON_DISCOUNT_TYPES}",
                          code="bad_discount_type")
    value = int(discount_value or 0)
    if discount_type == "percent" and not (1 <= value <= 100):
        raise CouponError("percent discount must be between 1 and 100", code="bad_percent")
    if discount_type == "fixed" and value <= 0:
        raise CouponError("fixed discount must be positive", code="bad_fixed")
    if product_type and product_type not in COUPON_PRODUCT_TYPES:
        raise CouponError("invalid product_type restriction", code="bad_product_type")
    if applies_to_action and applies_to_action not in COUPON_ACTIONS:
        raise CouponError("invalid action restriction", code="bad_action")


async def create_coupon(
    session: AsyncSession, *, code: str, discount_type: str, discount_value: int,
    title: str | None = None, description: str | None = None,
    max_discount_amount: int | None = None, min_order_amount: int | None = None,
    usage_limit: int | None = None, usage_limit_per_user: int | None = None,
    starts_at: datetime | None = None, expires_at: datetime | None = None,
    is_active: bool = True, product_id: int | None = None, product_type: str | None = None,
    applies_to_action: str | None = None, admin_id: int | None = None,
) -> Coupon:
    code = normalize_code(code)
    if not code:
        raise CouponError("code is required", code="code_required")
    _validate_fields(discount_type, discount_value, product_type, applies_to_action)
    if await get_by_code(session, code) is not None:
        raise CouponError("a coupon with this code already exists", code="code_exists")
    # Normalize "any" sentinels to NULL (no restriction).
    product_type = None if product_type in (None, "", "any") else product_type
    applies_to_action = None if applies_to_action in (None, "", "any") else applies_to_action
    coupon = Coupon(
        code=code, title=(title or None), description=(description or None),
        discount_type=discount_type, discount_value=int(discount_value),
        max_discount_amount=max_discount_amount, min_order_amount=min_order_amount,
        usage_limit=usage_limit, usage_limit_per_user=usage_limit_per_user,
        starts_at=starts_at, expires_at=expires_at, is_active=bool(is_active),
        product_id=product_id, product_type=product_type,
        applies_to_action=applies_to_action, created_by_admin_id=admin_id,
    )
    session.add(coupon)
    await session.flush()
    await audit_service.log(
        session, actor_type="admin", actor_id=admin_id, action="coupon_created",
        target_type="coupon", target_id=coupon.id,
        new=f"code={code} {discount_type}={discount_value}",
    )
    await session.refresh(coupon)
    return coupon


async def update_coupon(
    session: AsyncSession, coupon_id: int, *, admin_id: int | None = None, **changes,
) -> Coupon | None:
    coupon = await get_coupon(session, coupon_id)
    if coupon is None:
        return None
    if "code" in changes and changes["code"] is not None:
        new_code = normalize_code(changes.pop("code"))
        if new_code and new_code != coupon.code:
            clash = await get_by_code(session, new_code)
            if clash is not None and clash.id != coupon.id:
                raise CouponError("a coupon with this code already exists", code="code_exists")
            coupon.code = new_code
    else:
        changes.pop("code", None)

    discount_type = changes.get("discount_type", coupon.discount_type)
    discount_value = changes.get("discount_value", coupon.discount_value)
    product_type = changes.get("product_type", coupon.product_type)
    applies_to_action = changes.get("applies_to_action", coupon.applies_to_action)
    _validate_fields(discount_type, discount_value, product_type, applies_to_action)

    for field in ("title", "description", "discount_type", "discount_value",
                  "max_discount_amount", "min_order_amount", "usage_limit",
                  "usage_limit_per_user", "starts_at", "expires_at", "is_active",
                  "product_id", "product_type", "applies_to_action"):
        if field in changes:
            value = changes[field]
            if field in ("product_type", "applies_to_action") and value in ("", "any"):
                value = None
            setattr(coupon, field, value)
    await audit_service.log(
        session, actor_type="admin", actor_id=admin_id, action="coupon_updated",
        target_type="coupon", target_id=coupon.id, new=f"code={coupon.code}",
    )
    await session.refresh(coupon)
    return coupon


async def deactivate_coupon(
    session: AsyncSession, coupon_id: int, *, admin_id: int | None = None
) -> Coupon | None:
    coupon = await get_coupon(session, coupon_id)
    if coupon is None:
        return None
    coupon.is_active = False
    await audit_service.log(
        session, actor_type="admin", actor_id=admin_id, action="coupon_deactivated",
        target_type="coupon", target_id=coupon.id, meta=f"code={coupon.code}",
    )
    await session.refresh(coupon)
    return coupon

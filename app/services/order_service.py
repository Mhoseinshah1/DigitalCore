"""Order lifecycle: creation, lookup, cancellation, and receipt-readiness.

Phase 3 scope: create an order (card-to-card only), move it to waiting_admin once
a receipt lands (see payment_service), and let users/admins list it. Approval,
delivery, and provisioning are later phases and deliberately absent here.

Validation failures raise `OrderError` (a ValueError subclass) carrying a `.code`
so the bot can show a precise, translated message.
"""
from __future__ import annotations

from datetime import datetime, timezone

from sqlalchemy import func, select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.settings_service import SettingsService
from app.models.order import Order
from app.models.product import Product
from app.services import audit_service


class OrderError(ValueError):
    """A user-facing reason an order cannot be created/acted on."""

    code = "order_error"

    def __init__(self, message: str, code: str | None = None) -> None:
        super().__init__(message)
        if code is not None:
            self.code = code


def _now() -> datetime:
    return datetime.now(timezone.utc)


async def generate_order_number(session: AsyncSession) -> str:
    """A readable, per-day sequential number, e.g. ``DC-20260705-000001``.

    Uniqueness is enforced by the DB; `create_order` retries on the rare race.
    """
    today = _now().strftime("%Y%m%d")
    prefix = f"DC-{today}-"
    count = await session.scalar(
        select(func.count(Order.id)).where(Order.order_number.like(f"{prefix}%"))
    )
    return f"{prefix}{(count or 0) + 1:06d}"


async def create_order(
    session: AsyncSession,
    user_id: int,
    product_id: int,
    *,
    payment_method: str = "card_to_card",
    user_note: str | None = None,
    action_type: str | None = None,
    target_service_id: int | None = None,
) -> Order:
    """Create a pending_payment order for a purchasable product.

    Raises OrderError when sales/card-to-card are off, the product is not
    purchasable, its price is invalid, or a V2Ray product lacks its XUI binding.

    A renew/add-traffic order (Phase 8) additionally carries ``action_type`` and
    ``target_service_id``: the product must be a matching service-action product,
    and the target V2RayService must belong to the buyer and not be deleted.
    Nothing is delivered here.
    """
    if payment_method not in ("card_to_card", "wallet"):
        raise OrderError("unsupported payment method", code="method_unsupported")

    product = await session.get(Product, product_id)
    if product is None:
        raise OrderError("product not found", code="product_unavailable")
    if not product.is_active or product.is_hidden:
        raise OrderError("product is not available", code="product_unavailable")

    svc = SettingsService(session)
    if not await svc.get_bool("sales_enabled", True):
        raise OrderError("sales are disabled", code="sales_disabled")
    if payment_method == "card_to_card" and not await svc.get_bool("card_to_card_enabled", True):
        raise OrderError("card-to-card is disabled", code="card_disabled")
    if payment_method == "wallet" and not (
        await svc.get_bool("wallet_enabled", True)
        and await svc.get_bool("wallet_payment_enabled", True)
    ):
        raise OrderError("wallet payment is disabled", code="wallet_disabled")

    price = int(product.price or 0)
    if price <= 0:
        raise OrderError("product price is not set", code="invalid_price")

    # Service-action orders (Phase 8): renew/add-traffic. Validate the product
    # supports the action and the target service belongs to the buyer.
    if action_type is not None:
        if action_type not in ("renew_service", "add_traffic"):
            raise OrderError("unknown service action", code="bad_action")
        if (product.type != "v2ray" or not product.applies_to_service
                or product.action_type != action_type):
            raise OrderError(
                "product does not support this action", code="product_action_mismatch")
        if not target_service_id:
            raise OrderError("no target service selected", code="no_target_service")
        from app.models.v2ray_service import V2RayService
        service = await session.get(V2RayService, target_service_id)
        if service is None or service.user_id != user_id:
            raise OrderError("not your service", code="not_your_service")
        if service.status == "deleted":
            raise OrderError("service is deleted", code="service_deleted")
    else:
        # A product that only modifies an existing service cannot be sold as a
        # standalone (new) order.
        if product.applies_to_service:
            raise OrderError(
                "this product requires an existing service", code="requires_service")
        # New V2Ray service orders must already be bound to a server + inbound
        # (Phase 2.1). We do NOT create the client yet — we only refuse to sell a
        # misconfigured one.
        if product.type == "v2ray" and (
                not product.xui_server_id or not product.xui_inbound_id):
            raise OrderError(
                "this V2Ray product is not fully configured", code="product_misconfigured")

    discount_amount = 0
    final_amount = price - discount_amount

    order: Order | None = None
    for _ in range(5):  # retry only on the (rare) order-number collision
        number = await generate_order_number(session)
        candidate = Order(
            order_number=number,
            user_id=user_id,
            product_id=product_id,
            amount=price,
            discount_amount=discount_amount,
            final_amount=final_amount,
            status="pending_payment",
            payment_method=payment_method,
            user_note=user_note,
            action_type=action_type,
            target_service_id=target_service_id,
        )
        session.add(candidate)
        try:
            await session.flush()
        except IntegrityError:
            await session.rollback()
            continue
        order = candidate
        break
    if order is None:
        raise OrderError("could not allocate an order number", code="order_error")

    await audit_service.log(
        session, actor_type="user", actor_id=user_id, action="order_created",
        target_type="order", target_id=order.id,
        new=f"number={order.order_number} product_id={product_id} amount={final_amount}",
    )
    await session.refresh(order)
    return order


async def get_order(session: AsyncSession, order_id: int) -> Order | None:
    return await session.get(Order, order_id)


async def get_order_by_number(session: AsyncSession, order_number: str) -> Order | None:
    return await session.scalar(select(Order).where(Order.order_number == order_number))


async def list_user_orders(
    session: AsyncSession, user_id: int, *, limit: int = 20, offset: int = 0
) -> list[Order]:
    stmt = (
        select(Order)
        .where(Order.user_id == user_id)
        .order_by(Order.id.desc())
        .limit(limit)
        .offset(offset)
    )
    return list((await session.execute(stmt)).scalars().all())


async def count_user_orders(session: AsyncSession, user_id: int) -> int:
    return int(await session.scalar(
        select(func.count(Order.id)).where(Order.user_id == user_id)
    ) or 0)


async def latest_pending_order(session: AsyncSession, user_id: int) -> Order | None:
    """The user's most recent order still awaiting a receipt (pending_payment)."""
    stmt = (
        select(Order)
        .where(Order.user_id == user_id, Order.status == "pending_payment")
        .order_by(Order.id.desc())
        .limit(1)
    )
    return await session.scalar(stmt)


async def list_pending_receipt_orders(
    session: AsyncSession, *, limit: int = 50, offset: int = 0
) -> list[Order]:
    """Orders that have a submitted receipt awaiting admin review (waiting_admin)."""
    stmt = (
        select(Order)
        .where(Order.status == "waiting_admin")
        .order_by(Order.id.desc())
        .limit(limit)
        .offset(offset)
    )
    return list((await session.execute(stmt)).scalars().all())


async def list_all_orders(
    session: AsyncSession, *, limit: int = 100, offset: int = 0
) -> list[Order]:
    stmt = select(Order).order_by(Order.id.desc()).limit(limit).offset(offset)
    return list((await session.execute(stmt)).scalars().all())


def ensure_order_can_receive_receipt(order: Order) -> None:
    """Raise unless the order is still pending_payment (the only receiptable state)."""
    if order.status != "pending_payment":
        raise OrderError("order is not awaiting a receipt", code="order_not_receivable")


async def mark_waiting_admin(session: AsyncSession, order_id: int) -> Order | None:
    """Move an order into the admin review queue. Does not commit."""
    order = await get_order(session, order_id)
    if order is None:
        return None
    order.status = "waiting_admin"
    return order


async def cancel_order(
    session: AsyncSession, order_id: int, *, user_id: int | None = None
) -> Order | None:
    """Cancel an active order. If user_id is given, it must own the order."""
    order = await get_order(session, order_id)
    if order is None:
        return None
    if user_id is not None and order.user_id != user_id:
        raise OrderError("not your order", code="not_your_order")
    if order.status not in ("pending_payment", "waiting_admin"):
        return order  # already terminal — nothing to cancel
    order.status = "cancelled"
    order.cancelled_at = _now()
    await audit_service.log(
        session, actor_type="user", actor_id=user_id, action="order_cancelled",
        target_type="order", target_id=order.id, new=f"number={order.order_number}",
    )
    await session.refresh(order)
    return order

"""Product management: validation, CRUD, visibility, audit logging.

Per-type validation: "v2ray" products require duration_days > 0 and
traffic_gb > 0; "license" products ignore those fields. Prices are integer
toman and must be >= 0. Every create/update/deactivate/hide is audit-logged.
"""
from __future__ import annotations

from typing import Any

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.models.product import PRODUCT_ACTION_TYPES, PRODUCT_TYPES, Product
from app.models.xui_inbound import XuiInbound
from app.models.xui_server import XuiServer
from app.schemas.product import ProductCreate, ProductUpdate
from app.services import audit_service

_EDITABLE_FIELDS = (
    "type",
    "title",
    "description",
    "price",
    "duration_days",
    "traffic_gb",
    "ip_limit",
    "server_id",
    "inbound_id",
    "xui_server_id",
    "xui_inbound_id",
    "is_active",
    "is_hidden",
    "sort_order",
    "action_type",
    "applies_to_service",
)


def validate_product(
    type_: str,
    title: str,
    price: int,
    duration_days: int | None,
    traffic_gb: int | None,
    xui_server_id: int | None = None,
    xui_inbound_id: int | None = None,
    *,
    action_type: str | None = None,
    applies_to_service: bool = False,
) -> None:
    """Raise ValueError when the field combination is not a valid product.

    (Field-level checks only; the XUI server/inbound records are verified against
    the database by `validate_xui_binding`.)

    A service-action product (``applies_to_service``, Phase 8) is a v2ray product
    that modifies an EXISTING service, so it needs no server/inbound binding: a
    ``renew_service`` product needs a duration, an ``add_traffic`` product needs
    traffic, and that is all.
    """
    if type_ not in PRODUCT_TYPES:
        raise ValueError(f"type must be one of {PRODUCT_TYPES}, got {type_!r}")
    if not (title or "").strip():
        raise ValueError("title must not be empty")
    if price is None or int(price) < 0:
        raise ValueError("price must be >= 0")

    if applies_to_service or action_type:
        if type_ != "v2ray":
            raise ValueError("only v2ray products can be service actions")
        if not applies_to_service or not action_type:
            raise ValueError(
                "a service-action product needs both applies_to_service and an action_type")
        if action_type not in PRODUCT_ACTION_TYPES or action_type == "new_service":
            raise ValueError("action_type must be renew_service or add_traffic")
        if action_type == "renew_service" and (not duration_days or int(duration_days) <= 0):
            raise ValueError("a renewal product requires duration_days > 0")
        if action_type == "add_traffic" and (not traffic_gb or int(traffic_gb) <= 0):
            raise ValueError("an add-traffic product requires traffic_gb > 0")
        # Service actions never bind a server/inbound (they reuse the service's).
        if xui_server_id is not None or xui_inbound_id is not None:
            raise ValueError("service-action products must not set an XUI server/inbound")
        return

    if type_ == "license":
        if xui_server_id is not None or xui_inbound_id is not None:
            raise ValueError("license products must not set an XUI server/inbound")
    if type_ == "v2ray":
        if not duration_days or int(duration_days) <= 0:
            raise ValueError("v2ray products require duration_days > 0")
        if not traffic_gb or int(traffic_gb) <= 0:
            raise ValueError("v2ray products require traffic_gb > 0")
        if not xui_server_id:
            raise ValueError("v2ray products require an XUI server")
        if not xui_inbound_id:
            raise ValueError("v2ray products require an XUI inbound")


async def validate_xui_binding(
    session: AsyncSession,
    type_: str,
    xui_server_id: int | None,
    xui_inbound_id: int | None,
    *,
    is_active: bool,
) -> None:
    """Verify the XUI binding against the DB (v2ray only). Raises ValueError.

    The inbound must belong to the chosen server; for an ACTIVE product both the
    server and inbound must themselves be active.
    """
    if type_ != "v2ray":
        return
    server = await session.get(XuiServer, xui_server_id)
    if server is None:
        raise ValueError("selected XUI server does not exist")
    inbound = await session.get(XuiInbound, xui_inbound_id)
    if inbound is None:
        raise ValueError("selected XUI inbound does not exist")
    if inbound.server_id != server.id:
        raise ValueError("the selected inbound does not belong to the selected server")
    if is_active and (not server.is_active or not inbound.is_active):
        raise ValueError("an active V2Ray product needs an active server and inbound")


async def get(session: AsyncSession, product_id: int) -> Product | None:
    return await session.get(Product, product_id)


async def list_for_admin(session: AsyncSession) -> list[Product]:
    result = await session.execute(
        select(Product).order_by(Product.sort_order, Product.id)
    )
    return list(result.scalars().all())


async def list_for_user(session: AsyncSession) -> list[Product]:
    """Purchasable catalog products: active, not hidden, and NOT service-actions.

    Renew/add-traffic products (``applies_to_service``) are excluded here — they
    are only reachable from a specific service in /my_services (Phase 8)."""
    result = await session.execute(
        select(Product)
        .where(Product.is_active.is_(True), Product.is_hidden.is_(False),
               Product.applies_to_service.is_(False))
        .order_by(Product.sort_order, Product.id)
    )
    return list(result.scalars().all())


async def list_service_action_products(
    session: AsyncSession, action_type: str
) -> list[Product]:
    """Active, non-hidden v2ray products for a given service action (Phase 8)."""
    result = await session.execute(
        select(Product)
        .where(Product.is_active.is_(True), Product.is_hidden.is_(False),
               Product.type == "v2ray", Product.applies_to_service.is_(True),
               Product.action_type == action_type)
        .order_by(Product.sort_order, Product.id)
    )
    return list(result.scalars().all())


async def create(
    session: AsyncSession,
    data: ProductCreate,
    *,
    actor_type: str = "system",
    actor_id: int | None = None,
) -> Product:
    validate_product(
        data.type, data.title, data.price, data.duration_days, data.traffic_gb,
        data.xui_server_id, data.xui_inbound_id,
        action_type=data.action_type, applies_to_service=data.applies_to_service,
    )
    # Service-action products reuse the target service's binding, so they have
    # none of their own — skip the server/inbound DB verification for them.
    if not data.applies_to_service:
        await validate_xui_binding(
            session, data.type, data.xui_server_id, data.xui_inbound_id,
            is_active=data.is_active,
        )
    product = Product(
        type=data.type,
        title=data.title.strip(),
        description=data.description,
        price=int(data.price),
        duration_days=data.duration_days,
        traffic_gb=data.traffic_gb,
        ip_limit=data.ip_limit,
        server_id=data.server_id,
        inbound_id=data.inbound_id,
        xui_server_id=data.xui_server_id,
        xui_inbound_id=data.xui_inbound_id,
        is_active=data.is_active,
        is_hidden=data.is_hidden,
        sort_order=data.sort_order,
        action_type=data.action_type,
        applies_to_service=data.applies_to_service,
    )
    session.add(product)
    await session.flush()
    await audit_service.log(
        session,
        actor_type=actor_type,
        actor_id=actor_id,
        action="product.created",
        target_type="product",
        target_id=product.id,
        new=f"type={product.type} title={product.title!r} price={product.price}",
    )
    if product.type == "v2ray":
        await audit_service.log(
            session,
            actor_type=actor_type,
            actor_id=actor_id,
            action="product_bound_to_xui",
            target_type="product",
            target_id=product.id,
            new=f"xui_server_id={product.xui_server_id} xui_inbound_id={product.xui_inbound_id}",
        )
    await session.refresh(product)
    return product


async def update(
    session: AsyncSession,
    product_id: int,
    data: ProductUpdate,
    *,
    actor_type: str = "system",
    actor_id: int | None = None,
) -> Product | None:
    product = await get(session, product_id)
    if product is None:
        return None

    changes = data.model_dump(exclude_unset=True)
    changes = {k: v for k, v in changes.items() if k in _EDITABLE_FIELDS}
    if not changes:
        return product

    # Validate the MERGED state so a partial update cannot break invariants.
    merged: dict[str, Any] = {
        "type": product.type,
        "title": product.title,
        "price": product.price,
        "duration_days": product.duration_days,
        "traffic_gb": product.traffic_gb,
        "xui_server_id": product.xui_server_id,
        "xui_inbound_id": product.xui_inbound_id,
        "is_active": product.is_active,
        "action_type": product.action_type,
        "applies_to_service": product.applies_to_service,
    }
    merged.update({k: v for k, v in changes.items() if k in merged})
    validate_product(
        merged["type"],
        merged["title"],
        merged["price"],
        merged["duration_days"],
        merged["traffic_gb"],
        merged["xui_server_id"],
        merged["xui_inbound_id"],
        action_type=merged["action_type"],
        applies_to_service=bool(merged["applies_to_service"]),
    )
    if not merged["applies_to_service"]:
        await validate_xui_binding(
            session, merged["type"], merged["xui_server_id"], merged["xui_inbound_id"],
            is_active=bool(merged["is_active"]),
        )

    old_parts: list[str] = []
    new_parts: list[str] = []
    for key, new_value in changes.items():
        old_value = getattr(product, key)
        if old_value != new_value:
            old_parts.append(f"{key}={old_value!r}")
            new_parts.append(f"{key}={new_value!r}")
        setattr(product, key, new_value)

    if not new_parts:
        await session.commit()
        return product

    await audit_service.log(
        session,
        actor_type=actor_type,
        actor_id=actor_id,
        action="product.updated",
        target_type="product",
        target_id=product.id,
        old=", ".join(old_parts),
        new=", ".join(new_parts),
    )
    await session.refresh(product)
    return product


async def set_active(
    session: AsyncSession,
    product_id: int,
    active: bool,
    *,
    actor_type: str = "system",
    actor_id: int | None = None,
) -> Product | None:
    """Idempotently (de)activate. Deactivation is the soft-delete of this phase."""
    product = await get(session, product_id)
    if product is None:
        return None
    if product.is_active == active:
        return product
    product.is_active = active
    await audit_service.log(
        session,
        actor_type=actor_type,
        actor_id=actor_id,
        action="product.activated" if active else "product.deactivated",
        target_type="product",
        target_id=product.id,
        old=str(not active),
        new=str(active),
    )
    return product


async def deactivate(
    session: AsyncSession,
    product_id: int,
    *,
    actor_type: str = "system",
    actor_id: int | None = None,
) -> Product | None:
    return await set_active(
        session, product_id, False, actor_type=actor_type, actor_id=actor_id
    )


async def set_hidden(
    session: AsyncSession,
    product_id: int,
    hidden: bool,
    *,
    actor_type: str = "system",
    actor_id: int | None = None,
) -> Product | None:
    """Idempotent soft-hide: keeps the product but removes it from the user list."""
    product = await get(session, product_id)
    if product is None:
        return None
    if product.is_hidden == hidden:
        return product
    product.is_hidden = hidden
    await audit_service.log(
        session,
        actor_type=actor_type,
        actor_id=actor_id,
        action="product.hidden" if hidden else "product.unhidden",
        target_type="product",
        target_id=product.id,
        old=str(not hidden),
        new=str(hidden),
    )
    return product

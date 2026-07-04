"""Product management: validation, CRUD, visibility, audit logging.

Per-type validation: "v2ray" products require duration_days > 0 and
traffic_gb > 0; "license" products ignore those fields. Prices are integer
toman and must be >= 0. Every create/update/deactivate/hide is audit-logged.
"""
from __future__ import annotations

from typing import Any

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.models.product import PRODUCT_TYPES, Product
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
    "is_active",
    "is_hidden",
    "sort_order",
)


def validate_product(
    type_: str,
    title: str,
    price: int,
    duration_days: int | None,
    traffic_gb: int | None,
) -> None:
    """Raise ValueError when the combination is not a valid product."""
    if type_ not in PRODUCT_TYPES:
        raise ValueError(f"type must be one of {PRODUCT_TYPES}, got {type_!r}")
    if not (title or "").strip():
        raise ValueError("title must not be empty")
    if price is None or int(price) < 0:
        raise ValueError("price must be >= 0")
    if type_ == "v2ray":
        if not duration_days or int(duration_days) <= 0:
            raise ValueError("v2ray products require duration_days > 0")
        if not traffic_gb or int(traffic_gb) <= 0:
            raise ValueError("v2ray products require traffic_gb > 0")


async def get(session: AsyncSession, product_id: int) -> Product | None:
    return await session.get(Product, product_id)


async def list_for_admin(session: AsyncSession) -> list[Product]:
    result = await session.execute(
        select(Product).order_by(Product.sort_order, Product.id)
    )
    return list(result.scalars().all())


async def list_for_user(session: AsyncSession) -> list[Product]:
    """Only purchasable-looking products: active AND not hidden."""
    result = await session.execute(
        select(Product)
        .where(Product.is_active.is_(True), Product.is_hidden.is_(False))
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
    validate_product(data.type, data.title, data.price, data.duration_days, data.traffic_gb)
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
        is_active=data.is_active,
        is_hidden=data.is_hidden,
        sort_order=data.sort_order,
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
    }
    merged.update({k: v for k, v in changes.items() if k in merged})
    validate_product(
        merged["type"],
        merged["title"],
        merged["price"],
        merged["duration_days"],
        merged["traffic_gb"],
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

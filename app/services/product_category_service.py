"""Product categories: admin CRUD + the bot's category-first browse grouping.

A category is a simple admin-managed grouping so the bot shows *categories*
first, then the products inside a category, then a product's detail/invoice.

Visibility rules (bot):
  * a product is browsable when it is active, not hidden, and not a service-action
    (renew/add-traffic products are reached from /my_services, not the catalog);
  * only ACTIVE categories appear as browse buttons;
  * a browsable product whose category is NULL — or points at an inactive/deleted
    category — is shown under the synthetic "سایر محصولات" (Other) group so no
    purchasable product is ever orphaned;
  * categories sort by (sort_order, title); products by (sort_order, id).

The synthetic Other group has ``category_id is None``; the bot localizes its
title (i18n key ``products.category.other``).
"""
from __future__ import annotations

import re
from dataclasses import dataclass, field

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.models.product import Product
from app.models.product_category import ProductCategory
from app.services import audit_service


class CategoryError(ValueError):
    """Raised on invalid category input (e.g. empty title)."""

    def __init__(self, message: str, *, code: str = "invalid") -> None:
        super().__init__(message)
        self.code = code


@dataclass
class CategoryGroup:
    """A browsable group for the bot: a real category, or the synthetic Other."""

    category_id: int | None  # None == the synthetic "سایر محصولات" group
    title: str | None        # category.title, or None for Other (bot localizes)
    products: list[Product] = field(default_factory=list)


# --------------------------------------------------------------------------
# Slug helpers (mirrors tutorial_service so Persian titles keep their letters)
# --------------------------------------------------------------------------
def slugify(text: str) -> str:
    s = (text or "").strip().lower()
    s = re.sub(r"[\s_]+", "-", s)
    s = re.sub(r"[^\w\-]", "", s, flags=re.UNICODE)
    s = re.sub(r"-{2,}", "-", s).strip("-")
    return s or "category"


async def _unique_slug(
    session: AsyncSession, base: str, *, exclude_id: int | None = None
) -> str:
    slug = base
    n = 1
    while True:
        stmt = select(ProductCategory.id).where(ProductCategory.slug == slug)
        if exclude_id is not None:
            stmt = stmt.where(ProductCategory.id != exclude_id)
        clash = await session.scalar(stmt)
        if clash is None:
            return slug
        n += 1
        slug = f"{base}-{n}"


# --------------------------------------------------------------------------
# CRUD
# --------------------------------------------------------------------------
async def get(session: AsyncSession, category_id: int) -> ProductCategory | None:
    return await session.get(ProductCategory, category_id)


async def list_all(session: AsyncSession) -> list[ProductCategory]:
    stmt = select(ProductCategory).order_by(
        ProductCategory.sort_order, ProductCategory.title
    )
    return list((await session.execute(stmt)).scalars().all())


async def list_active(session: AsyncSession) -> list[ProductCategory]:
    stmt = (
        select(ProductCategory)
        .where(ProductCategory.is_active.is_(True))
        .order_by(ProductCategory.sort_order, ProductCategory.title)
    )
    return list((await session.execute(stmt)).scalars().all())


async def create(
    session: AsyncSession,
    *,
    title: str,
    description: str | None = None,
    sort_order: int = 0,
    is_active: bool = True,
    actor_type: str = "admin",
    actor_id: int | None = None,
) -> ProductCategory:
    title = (title or "").strip()
    if not title:
        raise CategoryError("title is required", code="title_required")
    slug = await _unique_slug(session, slugify(title))
    category = ProductCategory(
        title=title[:200],
        slug=slug,
        description=(description or None),
        sort_order=int(sort_order or 0),
        is_active=bool(is_active),
    )
    session.add(category)
    await session.flush()
    await audit_service.log(
        session,
        actor_type=actor_type,
        actor_id=actor_id,
        action="product_category.created",
        target_type="product_category",
        target_id=category.id,
        new=f"title={title[:60]!r} slug={slug}",
    )
    await session.refresh(category)
    return category


async def update(
    session: AsyncSession,
    category_id: int,
    *,
    title: str | None = None,
    description: str | None = None,
    sort_order: int | None = None,
    is_active: bool | None = None,
    actor_type: str = "admin",
    actor_id: int | None = None,
) -> ProductCategory | None:
    category = await get(session, category_id)
    if category is None:
        return None
    if title is not None:
        title = title.strip()
        if not title:
            raise CategoryError("title is required", code="title_required")
        if title[:200] != category.title:
            category.title = title[:200]
            category.slug = await _unique_slug(
                session, slugify(title), exclude_id=category.id
            )
    if description is not None:
        category.description = description.strip() or None
    if sort_order is not None:
        category.sort_order = int(sort_order)
    if is_active is not None:
        category.is_active = bool(is_active)
    await audit_service.log(
        session,
        actor_type=actor_type,
        actor_id=actor_id,
        action="product_category.updated",
        target_type="product_category",
        target_id=category.id,
        new=f"title={category.title[:60]!r} active={category.is_active}",
    )
    await session.refresh(category)
    return category


async def set_active(
    session: AsyncSession,
    category_id: int,
    active: bool,
    *,
    actor_type: str = "admin",
    actor_id: int | None = None,
) -> ProductCategory | None:
    category = await get(session, category_id)
    if category is None:
        return None
    if category.is_active == active:
        return category
    category.is_active = active
    await audit_service.log(
        session,
        actor_type=actor_type,
        actor_id=actor_id,
        action="product_category.activated" if active else "product_category.deactivated",
        target_type="product_category",
        target_id=category.id,
        old=str(not active),
        new=str(active),
    )
    await session.refresh(category)
    return category


async def toggle_active(
    session: AsyncSession,
    category_id: int,
    *,
    actor_type: str = "admin",
    actor_id: int | None = None,
) -> ProductCategory | None:
    category = await get(session, category_id)
    if category is None:
        return None
    return await set_active(
        session, category_id, not category.is_active,
        actor_type=actor_type, actor_id=actor_id,
    )


async def count_products(session: AsyncSession, category_id: int) -> int:
    """How many products reference this category (any state) — admin display."""
    rows = await session.execute(
        select(Product.id).where(Product.category_id == category_id)
    )
    return len(list(rows.scalars().all()))


# --------------------------------------------------------------------------
# Bot browse grouping
# --------------------------------------------------------------------------
async def _browsable_products(session: AsyncSession) -> list[Product]:
    """Active, non-hidden, non-service-action products in display order."""
    stmt = (
        select(Product)
        .where(
            Product.is_active.is_(True),
            Product.is_hidden.is_(False),
            Product.applies_to_service.is_(False),
        )
        .order_by(Product.sort_order, Product.id)
    )
    return list((await session.execute(stmt)).scalars().all())


async def grouped_for_bot(session: AsyncSession) -> list[CategoryGroup]:
    """Ordered browse groups: active categories with products, then Other.

    A browsable product whose category_id is NULL, or points at an inactive or
    deleted category, falls into the synthetic Other group so it stays reachable.
    """
    products = await _browsable_products(session)
    active = await list_active(session)  # already (sort_order, title) ordered
    active_by_id = {c.id: c for c in active}

    grouped: dict[int, list[Product]] = {c.id: [] for c in active}
    other: list[Product] = []
    for product in products:
        cid = product.category_id
        if cid is not None and cid in active_by_id:
            grouped[cid].append(product)
        else:
            other.append(product)

    result: list[CategoryGroup] = []
    for category in active:
        items = grouped[category.id]
        if items:
            result.append(
                CategoryGroup(category_id=category.id, title=category.title, products=items)
            )
    if other:
        result.append(CategoryGroup(category_id=None, title=None, products=other))
    return result


async def products_for_category(
    session: AsyncSession, category_id: int | None
) -> list[Product]:
    """Browsable products for one group (None == the Other group).

    Kept consistent with grouped_for_bot: an inactive/deleted category yields the
    empty list, and Other collects NULL/inactive-category products.
    """
    for group in await grouped_for_bot(session):
        if group.category_id == category_id:
            return group.products
    return []

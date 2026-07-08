"""Bot UX: ProductCategory service — CRUD, slug uniqueness, and the bot grouping
(active categories with products, Other fallback, inactive/hidden handling)."""
from __future__ import annotations

import pytest_asyncio
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker, create_async_engine
from sqlalchemy.pool import StaticPool

from app.models import Base
from app.schemas.product import ProductCreate
from app.services import product_category_service as pcs
from app.services import product_service


@pytest_asyncio.fixture
async def db():
    engine = create_async_engine("sqlite+aiosqlite://", poolclass=StaticPool,
                                 connect_args={"check_same_thread": False})
    async with engine.begin() as conn:
        await conn.run_sync(Base.metadata.create_all)
    maker = async_sessionmaker(engine, class_=AsyncSession, expire_on_commit=False)
    try:
        yield maker
    finally:
        await engine.dispose()


async def _mk_product(s, title, *, category_id=None, hidden=False, active=True):
    return await product_service.create(
        s, ProductCreate(type="license", title=title, price=1000,
                         category_id=category_id, is_hidden=hidden, is_active=active))


async def test_create_generates_unique_slugs(db) -> None:
    async with db() as s:
        a = await pcs.create(s, title="لایسنس‌ها")
        b = await pcs.create(s, title="لایسنس‌ها")  # same title
        await s.commit()
    assert a.slug and b.slug and a.slug != b.slug


async def test_toggle_active(db) -> None:
    async with db() as s:
        c = await pcs.create(s, title="Cat")
        await s.commit()
        assert c.is_active is True
        c2 = await pcs.toggle_active(s, c.id)
        await s.commit()
    assert c2.is_active is False


async def test_product_category_validation(db) -> None:
    async with db() as s:
        # A bad category id is rejected on product create.
        try:
            await _mk_product(s, "bad", category_id=999)
            assert False, "should have raised"
        except ValueError as exc:
            assert "category" in str(exc)


async def test_grouping_active_inactive_and_other(db) -> None:
    async with db() as s:
        c_active = await pcs.create(s, title="Active", sort_order=1)
        c_empty = await pcs.create(s, title="Empty", sort_order=0)
        c_inactive = await pcs.create(s, title="Inactive", is_active=False)
        await s.commit()
        await _mk_product(s, "P-active", category_id=c_active.id)
        await _mk_product(s, "P-uncat")                       # NULL category
        await _mk_product(s, "P-in-inactive", category_id=c_inactive.id)
        await _mk_product(s, "P-hidden", category_id=c_active.id, hidden=True)
        await s.commit()

        groups = await pcs.grouped_for_bot(s)
        by_id = {g.category_id: g for g in groups}

    # Active category with a visible product appears; empty active one does not.
    assert c_active.id in by_id
    assert c_empty.id not in by_id
    # Inactive category is not a browse group.
    assert c_inactive.id not in by_id
    # Uncategorised + inactive-category products collapse into the Other group.
    assert None in by_id
    other_titles = {p.title for p in by_id[None].products}
    assert other_titles == {"P-uncat", "P-in-inactive"}
    # Hidden products never show.
    active_titles = {p.title for p in by_id[c_active.id].products}
    assert active_titles == {"P-active"}


async def test_products_for_category_matches_group(db) -> None:
    async with db() as s:
        c = await pcs.create(s, title="C")
        await s.commit()
        await _mk_product(s, "in-c", category_id=c.id)
        await _mk_product(s, "uncat")
        await s.commit()
        in_c = await pcs.products_for_category(s, c.id)
        other = await pcs.products_for_category(s, None)
    assert [p.title for p in in_c] == ["in-c"]
    assert [p.title for p in other] == ["uncat"]

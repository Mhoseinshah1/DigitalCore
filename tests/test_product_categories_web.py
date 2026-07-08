"""Web admin: product-category CRUD + product-form category dropdown + RBAC."""
from __future__ import annotations

from collections.abc import AsyncIterator, Awaitable, Callable

import httpx
import pytest_asyncio
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker, create_async_engine
from sqlalchemy.pool import StaticPool

from app.core.security import hash_password
from app.database import get_session
from app.models import Admin, Base
from app.web.main import app

PASSWORD = "cat-web-1"
ClientFactory = Callable[[str], Awaitable[httpx.AsyncClient]]


@pytest_asyncio.fixture
async def client_with_role() -> AsyncIterator[ClientFactory]:
    engine = create_async_engine("sqlite+aiosqlite://", poolclass=StaticPool,
                                 connect_args={"check_same_thread": False})
    async with engine.begin() as conn:
        await conn.run_sync(Base.metadata.create_all)
    maker = async_sessionmaker(engine, class_=AsyncSession, expire_on_commit=False)

    async def _override_session():
        async with maker() as session:
            yield session

    app.dependency_overrides[get_session] = _override_session
    transport = httpx.ASGITransport(app=app)
    clients: list[httpx.AsyncClient] = []

    async def factory(role: str) -> httpx.AsyncClient:
        username = f"catweb_{role}"
        async with maker() as s:
            s.add(Admin(username=username, password_hash=hash_password(PASSWORD),
                        is_active=True, is_super_admin=(role == "owner"), role=role))
            await s.commit()
        client = httpx.AsyncClient(transport=transport, base_url="http://testserver")
        clients.append(client)
        r = await client.post("/admin/login",
                              data={"username": username, "password": PASSWORD},
                              follow_redirects=False)
        assert r.status_code == 302
        return client

    try:
        yield factory
    finally:
        for c in clients:
            await c.aclose()
        app.dependency_overrides.pop(get_session, None)
        await engine.dispose()


async def test_viewer_cannot_manage_categories(client_with_role) -> None:
    client = await client_with_role("viewer")
    r = await client.get("/admin/product-categories")
    assert r.status_code == 403
    r = await client.post("/admin/product-categories/create",
                          data={"title": "X", "is_active": "on"}, follow_redirects=False)
    assert r.status_code == 403


async def test_admin_crud_category_and_product_dropdown(client_with_role) -> None:
    client = await client_with_role("admin")

    # Empty list renders.
    r = await client.get("/admin/product-categories")
    assert r.status_code == 200

    # Create.
    r = await client.post("/admin/product-categories/create",
                          data={"title": "لایسنس‌ها", "description": "d",
                                "sort_order": "2", "is_active": "on"},
                          follow_redirects=False)
    assert r.status_code == 303 and "saved=created" in r.headers["location"]

    r = await client.get("/admin/product-categories")
    assert "لایسنس‌ها" in r.text

    # Edit (category id 1 on a fresh DB).
    r = await client.post("/admin/product-categories/1/edit",
                          data={"title": "لایسنس‌ها ویرایش", "sort_order": "3", "is_active": "on"},
                          follow_redirects=False)
    assert r.status_code == 303 and "saved=updated" in r.headers["location"]

    # Toggle active.
    r = await client.post("/admin/product-categories/1/toggle-active", follow_redirects=False)
    assert r.status_code == 303

    # The product create form offers the category in its dropdown.
    r = await client.get("/admin/products/create")
    assert r.status_code == 200 and "لایسنس‌ها ویرایش" in r.text

    # Creating a product with that category shows the category in the product list.
    r = await client.post("/admin/products/create",
                          data={"type": "license", "title": "Keyed", "price": "1000",
                                "category_id": "1", "is_active": "on"},
                          follow_redirects=False)
    assert r.status_code == 303 and "saved=1" in r.headers["location"]
    r = await client.get("/admin/products")
    assert "Keyed" in r.text and "لایسنس‌ها ویرایش" in r.text


async def test_bad_category_id_on_product_is_rejected(client_with_role) -> None:
    client = await client_with_role("owner")
    r = await client.post("/admin/products/create",
                          data={"type": "license", "title": "P", "price": "1000",
                                "category_id": "999", "is_active": "on"},
                          follow_redirects=False)
    assert r.status_code == 303 and "error=" in r.headers["location"]

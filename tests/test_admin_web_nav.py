"""Admin web navigation: dashboard (+ /dashboard redirect), users, notifications,
and that the sidebar links point to routes that actually resolve."""
from __future__ import annotations

from collections.abc import AsyncIterator

import httpx
import pytest_asyncio
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker, create_async_engine
from sqlalchemy.pool import StaticPool

from app.core.security import hash_password
from app.database import get_session
from app.models import Base, Admin, User
from app.web.main import app
from app.web.views import NAV_TREE

PASSWORD = "nav-web-1"


@pytest_asyncio.fixture
async def ctx() -> AsyncIterator[tuple[httpx.AsyncClient, async_sessionmaker]]:
    engine = create_async_engine("sqlite+aiosqlite://", poolclass=StaticPool,
                                 connect_args={"check_same_thread": False})
    async with engine.begin() as conn:
        await conn.run_sync(Base.metadata.create_all)
    maker = async_sessionmaker(engine, class_=AsyncSession, expire_on_commit=False)

    async def _override():
        async with maker() as s:
            yield s

    app.dependency_overrides[get_session] = _override
    async with maker() as s:
        s.add(Admin(username="owner", password_hash=hash_password(PASSWORD),
                    is_active=True, is_super_admin=True, role="owner"))
        await s.commit()
    transport = httpx.ASGITransport(app=app)
    c = httpx.AsyncClient(transport=transport, base_url="http://t")
    r = await c.post("/admin/login", data={"username": "owner", "password": PASSWORD},
                     follow_redirects=False)
    assert r.status_code == 302
    try:
        yield c, maker
    finally:
        await c.aclose()
        app.dependency_overrides.pop(get_session, None)
        await engine.dispose()


async def test_dashboard_works_empty_db(ctx) -> None:
    client, _maker = ctx
    r = await client.get("/admin")
    assert r.status_code == 200


async def test_dashboard_alias_redirects(ctx) -> None:
    client, _maker = ctx
    r = await client.get("/admin/dashboard", follow_redirects=False)
    assert r.status_code == 302 and r.headers["location"] == "/admin"


async def test_users_empty_and_populated(ctx) -> None:
    client, maker = ctx
    r = await client.get("/admin/users")
    assert r.status_code == 200
    async with maker() as s:
        s.add(User(telegram_id=999, first_name="Someone"))
        await s.commit()
    r = await client.get("/admin/users")
    assert r.status_code == 200 and "Someone" in r.text


async def test_notifications_page_renders(ctx) -> None:
    client, _maker = ctx
    r = await client.get("/admin/notifications")
    assert r.status_code == 200
    assert "اطلاع" in r.text  # Persian placeholder copy


def test_notifications_and_dashboard_in_nav_tree() -> None:
    hrefs: list[str] = []
    for node in NAV_TREE:
        if "children" in node:
            hrefs += [c["href"] for c in node["children"]]
        elif "href" in node:
            hrefs.append(node["href"])
    assert "/admin/notifications" in hrefs
    assert "/admin" in hrefs


async def test_key_sidebar_links_resolve(ctx) -> None:
    client, _maker = ctx
    for href in ("/admin", "/admin/users", "/admin/notifications", "/admin/dashboard"):
        r = await client.get(href, follow_redirects=False)
        assert r.status_code in (200, 302, 303), f"{href} -> {r.status_code}"

"""Admin UI redesign smoke tests.

Route health + structural checks for the redesigned admin panel: the theme CSS
is served and linked, the dashboard renders the new components, list pages render
(including their empty states), and every RBAC-filtered sidebar link for an owner
resolves (no dead links). These assert structure/health, not pixel styling.
"""
from __future__ import annotations

from collections.abc import AsyncIterator

import httpx
import pytest_asyncio
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker, create_async_engine
from sqlalchemy.pool import StaticPool

from app.core.security import hash_password
from app.database import get_session
from app.models import Admin, Base
from app.web.main import app
from app.web.views import build_nav

PASSWORD = "ui-redesign-1"


@pytest_asyncio.fixture
async def client() -> AsyncIterator[httpx.AsyncClient]:
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
        yield c
    finally:
        await c.aclose()
        app.dependency_overrides.pop(get_session, None)
        await engine.dispose()


async def test_theme_css_is_served(client) -> None:
    r = await client.get("/static/css/admin-theme.css")
    assert r.status_code == 200
    body = r.text
    # Design tokens + a couple of new components are present.
    assert "--dc-primary" in body
    assert ".empty-state" in body
    assert "theme-dark" in body


async def test_base_links_theme_and_has_toggle(client) -> None:
    r = await client.get("/admin")
    assert r.status_code == 200
    assert "/static/css/admin-theme.css" in r.text
    assert 'id="themeToggle"' in r.text          # dark/light toggle present
    assert "theme-dark" in r.text                # early theme-init script present


async def test_dashboard_has_new_components(client) -> None:
    r = await client.get("/admin")
    assert r.status_code == 200
    # KPI cards + quick actions (owner can see the quick grid).
    assert "stat-grid" in r.text
    assert "quick-grid" in r.text
    # Active sidebar state is marked for the current page.
    assert 'aria-current="page"' in r.text


async def test_products_empty_state_renders(client) -> None:
    r = await client.get("/admin/products")
    assert r.status_code == 200
    assert "empty-state" in r.text  # macro-based empty state, no crash on 0 rows


async def test_key_pages_render_200(client) -> None:
    pages = [
        "/admin", "/admin/users", "/admin/products", "/admin/product-categories",
        "/admin/orders", "/admin/xui-servers", "/admin/xui-inbounds",
        "/admin/v2ray-services", "/admin/licenses", "/admin/tickets",
        "/admin/wallet/topups", "/admin/coupons", "/admin/tutorials",
        "/admin/reports", "/admin/notifications", "/admin/settings/general",
    ]
    for href in pages:
        r = await client.get(href, follow_redirects=True)
        assert r.status_code == 200, f"{href} -> {r.status_code}"
        assert "/static/css/admin-theme.css" in r.text, f"{href} missing theme"


async def test_no_dead_sidebar_links_for_owner(client) -> None:
    # Every RBAC-filtered sidebar link an owner sees must resolve (no 404/500).
    hrefs: list[str] = []
    for sec in build_nav("owner", "fa", "/admin"):
        if sec.get("children"):
            hrefs += [it["href"] for it in sec["children"]]
        elif sec.get("href"):
            hrefs.append(sec["href"])
    assert hrefs, "owner should see sidebar links"
    for href in hrefs:
        path = href.split("#", 1)[0]  # drop in-page anchors
        r = await client.get(path, follow_redirects=False)
        assert r.status_code in (200, 302, 303), f"{href} -> {r.status_code}"

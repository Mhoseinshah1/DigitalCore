"""Admin-panel navigation shell: grouped RTL sidebar, placeholders, RBAC filtering.

Templates/nav only — no DB/model/service behaviour is exercised beyond login.
"""
from __future__ import annotations

from collections.abc import AsyncIterator, Awaitable, Callable

import httpx
import pytest
import pytest_asyncio
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker, create_async_engine
from sqlalchemy.pool import StaticPool

from app.core.permissions import Role
from app.core.security import hash_password
from app.database import get_session
from app.models import Admin, Base
from app.web.main import app
from app.web.views import build_nav

PASSWORD = "nav-shell-1"

ClientFactory = Callable[[str], Awaitable[httpx.AsyncClient]]


@pytest_asyncio.fixture
async def client_with_role() -> AsyncIterator[ClientFactory]:
    engine = create_async_engine(
        "sqlite+aiosqlite://",
        poolclass=StaticPool,
        connect_args={"check_same_thread": False},
    )
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
        username = f"navshell_{role}"
        async with maker() as s:
            s.add(
                Admin(
                    username=username,
                    password_hash=hash_password(PASSWORD),
                    is_active=True,
                    is_super_admin=(role == "owner"),
                    role=role,
                )
            )
            await s.commit()
        client = httpx.AsyncClient(transport=transport, base_url="http://testserver")
        clients.append(client)
        r = await client.post(
            "/login",
            data={"username": username, "password": PASSWORD},
            follow_redirects=False,
        )
        assert r.status_code == 302
        return client

    try:
        yield factory
    finally:
        for c in clients:
            await c.aclose()
        app.dependency_overrides.pop(get_session, None)
        await engine.dispose()


# --------------------------------------------------------------------------
# Shell markup + direction on the real pages
# --------------------------------------------------------------------------
REAL_PAGES = ["/", "/products", "/servers", "/settings"]


@pytest.mark.parametrize("path", REAL_PAGES)
async def test_real_pages_render_shell_rtl_by_default(client_with_role, path) -> None:
    client = await client_with_role("owner")
    r = await client.get(path)
    assert r.status_code == 200
    # Grouped sidebar shell is present.
    assert 'class="sidebar"' in r.text
    assert 'class="nav-tree"' in r.text
    assert 'class="topbar"' in r.text
    # fa is the default language -> RTL document.
    assert 'dir="rtl"' in r.text
    assert 'lang="fa"' in r.text


@pytest.mark.parametrize("path", REAL_PAGES)
async def test_real_pages_render_ltr_with_en_cookie(client_with_role, path) -> None:
    client = await client_with_role("owner")
    client.cookies.set("dc_lang", "en")
    r = await client.get(path)
    assert r.status_code == 200
    assert 'dir="ltr"' in r.text
    assert 'lang="en"' in r.text
    assert 'class="sidebar"' in r.text


async def test_servers_item_visible_under_its_group(client_with_role) -> None:
    """The 3X-UI/Sanaei servers link must be reachable from the sidebar."""
    client = await client_with_role("owner")
    client.cookies.set("dc_lang", "en")
    r = await client.get("/servers")
    assert r.status_code == 200
    assert "3X-UI servers (Sanaei)" in r.text
    assert 'href="/servers"' in r.text
    # Active-state highlight lands on the servers link.
    assert 'class="nav-link active" href="/servers"' in r.text


# --------------------------------------------------------------------------
# Placeholder pages
# --------------------------------------------------------------------------
async def test_placeholder_page_renders_coming_soon(client_with_role) -> None:
    client = await client_with_role("owner")
    client.cookies.set("dc_lang", "en")
    r = await client.get("/licenses")
    assert r.status_code == 200
    assert "coming soon" in r.text.lower()
    # Still the full shell, not a bare page.
    assert 'class="sidebar"' in r.text


async def test_placeholder_requires_login() -> None:
    transport = httpx.ASGITransport(app=app)
    async with httpx.AsyncClient(transport=transport, base_url="http://testserver") as c:
        r = await c.get("/licenses", follow_redirects=False)
    assert r.status_code == 302
    assert r.headers["location"] == "/login"


async def test_placeholder_respects_permission(client_with_role) -> None:
    # viewer lacks manage_products -> /licenses is forbidden even by direct URL.
    client = await client_with_role("viewer")
    r = await client.get("/licenses")
    assert r.status_code == 403


# --------------------------------------------------------------------------
# Nav-tree RBAC filtering (unit tests on the builder)
# --------------------------------------------------------------------------
def _all_hrefs(nav: list[dict]) -> set[str]:
    hrefs: set[str] = set()
    for sec in nav:
        if sec["href"]:
            hrefs.add(sec["href"])
        for child in sec["children"]:
            hrefs.add(child["href"])
    return hrefs


def test_nav_owner_sees_everything() -> None:
    hrefs = _all_hrefs(build_nav(Role.OWNER, "en", "/"))
    for expected in ("/", "/products", "/servers", "/orders", "/users", "/audit-logs"):
        assert expected in hrefs


def test_nav_viewer_is_reduced() -> None:
    nav = build_nav(Role.VIEWER, "en", "/")
    hrefs = _all_hrefs(nav)
    # Viewer only has view_dashboard.
    assert "/" in hrefs
    assert "/reports" in hrefs
    # Admin-only sections are hidden.
    for hidden in ("/products", "/servers", "/orders", "/users", "/audit-logs"):
        assert hidden not in hrefs
    # Groups with no visible child are dropped entirely (no empty Sales/3X-UI).
    labels = {sec["label"] for sec in nav}
    assert "Sales" not in labels
    assert "3X-UI servers (Sanaei)" not in labels


def test_nav_accountant_sees_orders_but_not_products() -> None:
    """approve_payments reveals Orders/Payments even without manage_products."""
    hrefs = _all_hrefs(build_nav(Role.ACCOUNTANT, "en", "/"))
    assert "/orders" in hrefs and "/payments" in hrefs
    assert "/products" not in hrefs


def test_nav_marks_active_section() -> None:
    nav = build_nav(Role.OWNER, "en", "/servers")
    xui = next(s for s in nav if s["label"].startswith("3X-UI"))
    assert xui["active"] is True
    servers = next(c for c in xui["children"] if c["href"] == "/servers")
    assert servers["active"] is True


def test_nav_placeholder_flags_set() -> None:
    nav = build_nav(Role.OWNER, "en", "/")
    flat = {c["href"]: c for s in nav for c in s["children"]}
    flat.update({s["href"]: s for s in nav if s["href"]})
    assert flat["/orders"]["placeholder"] is True
    assert flat["/products"]["placeholder"] is False
    assert flat["/servers"]["placeholder"] is False


def test_nav_empty_for_anonymous() -> None:
    assert build_nav(None, "en", "/") == []

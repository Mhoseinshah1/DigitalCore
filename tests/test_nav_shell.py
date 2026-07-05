"""Phase 2 admin shell: grouped RTL sidebar, placeholders, RBAC-filtered nav."""
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
            "/admin/login",
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


REAL_PAGES = ["/admin", "/admin/users", "/admin/products",
              "/admin/settings/general", "/admin/audit-logs"]


@pytest.mark.parametrize("path", REAL_PAGES)
async def test_real_pages_render_shell_rtl_by_default(client_with_role, path) -> None:
    client = await client_with_role("owner")
    r = await client.get(path)
    assert r.status_code == 200
    assert 'class="sidebar"' in r.text
    assert 'class="nav-tree"' in r.text
    assert 'class="topbar"' in r.text
    assert 'dir="rtl"' in r.text and 'lang="fa"' in r.text


@pytest.mark.parametrize("path", REAL_PAGES)
async def test_real_pages_render_ltr_with_en_cookie(client_with_role, path) -> None:
    client = await client_with_role("owner")
    client.cookies.set("dc_lang", "en")
    r = await client.get(path)
    assert r.status_code == 200
    assert 'dir="ltr"' in r.text and 'lang="en"' in r.text
    assert 'class="sidebar"' in r.text


async def test_placeholder_page_renders_coming_soon(client_with_role) -> None:
    client = await client_with_role("owner")
    client.cookies.set("dc_lang", "en")
    r = await client.get("/admin/tickets")
    assert r.status_code == 200
    assert "coming soon" in r.text.lower()
    assert 'class="sidebar"' in r.text


async def test_placeholder_requires_login() -> None:
    transport = httpx.ASGITransport(app=app)
    async with httpx.AsyncClient(transport=transport, base_url="http://testserver") as c:
        r = await c.get("/admin/tickets", follow_redirects=False)
    assert r.status_code == 302
    assert r.headers["location"] == "/admin/login"


async def test_placeholder_respects_permission(client_with_role) -> None:
    # viewer lacks manage_products -> /admin/licenses is forbidden.
    client = await client_with_role("viewer")
    r = await client.get("/admin/licenses")
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
    hrefs = _all_hrefs(build_nav(Role.OWNER, "en", "/admin"))
    for expected in ("/admin", "/admin/users", "/admin/products",
                     "/admin/settings/general", "/admin/settings/payment",
                     "/admin/audit-logs", "/admin/xui-servers", "/admin/xui-inbounds"):
        assert expected in hrefs


def test_nav_viewer_is_reduced() -> None:
    nav = build_nav(Role.VIEWER, "en", "/admin")
    hrefs = _all_hrefs(nav)
    assert "/admin" in hrefs
    assert "/admin/users" in hrefs
    for hidden in ("/admin/products", "/admin/settings/general",
                   "/admin/settings/payment", "/admin/audit-logs", "/admin/xui-servers"):
        assert hidden not in hrefs


def test_nav_accountant_sees_payments_not_settings() -> None:
    hrefs = _all_hrefs(build_nav(Role.ACCOUNTANT, "en", "/admin"))
    assert "/admin/settings/payment" in hrefs  # view_payments
    assert "/admin/users" in hrefs
    assert "/admin/settings/general" not in hrefs  # manage_settings
    assert "/admin/products" not in hrefs


def test_nav_support_can_reach_users_not_settings() -> None:
    hrefs = _all_hrefs(build_nav(Role.SUPPORT, "en", "/admin"))
    assert "/admin/users" in hrefs
    assert "/admin/settings/general" not in hrefs
    assert "/admin/settings/payment" not in hrefs


def test_nav_marks_active_section() -> None:
    nav = build_nav(Role.OWNER, "en", "/admin/users")
    users = next(s for s in nav if any(c["href"] == "/admin/users" for c in s["children"]))
    assert users["active"] is True
    child = next(c for c in users["children"] if c["href"] == "/admin/users")
    assert child["active"] is True


def test_nav_empty_for_anonymous() -> None:
    assert build_nav(None, "en", "/admin") == []

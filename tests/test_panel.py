"""Admin panel: reachability, cookie login flow, and settings round-trip."""
from __future__ import annotations

import httpx
import pytest_asyncio
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker, create_async_engine
from sqlalchemy.pool import StaticPool

from app.core.security import hash_password
from app.database import get_session
from app.models import Admin, Base
from app.web.main import app

ADMIN_USERNAME = "paneladmin"
ADMIN_EMAIL = "panel@test.io"
ADMIN_PASSWORD = "panel-pw-123"


# ---------------------------------------------------------------------------
# Anonymous reachability (no database needed)
# ---------------------------------------------------------------------------

async def test_login_page_renders_fa_rtl_by_default(client) -> None:
    r = await client.get("/login")
    assert r.status_code == 200
    assert "text/html" in r.headers["content-type"]
    # Default language is fa, rendered right-to-left.
    assert 'dir="rtl"' in r.text and 'lang="fa"' in r.text
    assert "ورود" in r.text  # Sign in (fa)
    assert "نام کاربری" in r.text  # Username (fa)
    assert 'name="username"' in r.text  # field names stay English


async def test_login_page_renders_en_ltr_with_cookie(client) -> None:
    r = await client.get("/login", cookies={"dc_lang": "en"})
    assert r.status_code == 200
    assert 'dir="ltr"' in r.text and 'lang="en"' in r.text
    assert "Sign in" in r.text
    assert "Username" in r.text


async def test_static_css_served(client) -> None:
    r = await client.get("/static/css/style.css")
    assert r.status_code == 200
    assert "text/css" in r.headers["content-type"]


async def test_dashboard_redirects_anonymous_to_login(client) -> None:
    r = await client.get("/", follow_redirects=False)
    assert r.status_code == 302
    assert r.headers["location"] == "/login"


async def test_settings_api_requires_auth(client) -> None:
    r = await client.get("/api/settings")
    assert r.status_code == 401


# ---------------------------------------------------------------------------
# Authenticated flow (in-memory SQLite via dependency override)
# ---------------------------------------------------------------------------

@pytest_asyncio.fixture
async def panel_client() -> httpx.AsyncClient:
    """Client against the real app with get_session overridden to a fresh
    in-memory SQLite database seeded with one admin (test-only create_all)."""
    engine = create_async_engine(
        "sqlite+aiosqlite://",
        poolclass=StaticPool,
        connect_args={"check_same_thread": False},
    )
    async with engine.begin() as conn:
        await conn.run_sync(Base.metadata.create_all)
    maker = async_sessionmaker(engine, class_=AsyncSession, expire_on_commit=False)
    async with maker() as s:
        s.add(
            Admin(
                username=ADMIN_USERNAME,
                email=ADMIN_EMAIL,
                password_hash=hash_password(ADMIN_PASSWORD),
                is_active=True,
                is_super_admin=True,
            )
        )
        await s.commit()

    async def _override_session():
        async with maker() as session:
            yield session

    app.dependency_overrides[get_session] = _override_session
    transport = httpx.ASGITransport(app=app)
    try:
        async with httpx.AsyncClient(transport=transport, base_url="http://testserver") as c:
            yield c
    finally:
        app.dependency_overrides.pop(get_session, None)
        await engine.dispose()


async def _login(client: httpx.AsyncClient) -> None:
    r = await client.post(
        "/login",
        data={"username": ADMIN_USERNAME, "password": ADMIN_PASSWORD},
        follow_redirects=False,
    )
    assert r.status_code == 302 and r.headers["location"] == "/"


async def test_form_login_sets_httponly_cookie_and_dashboard_renders(panel_client) -> None:
    r = await panel_client.post(
        "/login",
        data={"username": ADMIN_USERNAME, "password": ADMIN_PASSWORD},
        follow_redirects=False,
    )
    assert r.status_code == 302 and r.headers["location"] == "/"
    set_cookie = r.headers.get("set-cookie", "")
    assert "dc_session=" in set_cookie
    assert "httponly" in set_cookie.lower()
    # Plain-HTTP request => the cookie must NOT be Secure, or the browser drops
    # it and every post-login request bounces back to /login.
    assert "secure" not in set_cookie.lower()

    panel_client.cookies.set("dc_lang", "en")
    r = await panel_client.get("/")
    assert r.status_code == 200
    assert "Dashboard" in r.text
    # The sidebar identity shows the admin's username.
    assert ADMIN_USERNAME in r.text

    # And the same page renders in Persian when the language cookie says fa.
    panel_client.cookies.set("dc_lang", "fa")
    r = await panel_client.get("/")
    assert r.status_code == 200
    assert "داشبورد" in r.text and 'dir="rtl"' in r.text


async def test_cookie_not_secure_even_when_panel_url_is_https(panel_client, monkeypatch) -> None:
    """Regression: WEB_PANEL_URL=https://… must NOT force Secure on plain HTTP.

    The installer writes an https WEB_PANEL_URL while the panel is still served
    over http://host:8000; deciding Secure from that URL made the browser drop
    the cookie and login looped for any credentials.
    """
    from app.config import settings

    monkeypatch.setattr(settings, "WEB_PANEL_URL", "https://panel.example.com")
    monkeypatch.setattr(settings, "COOKIE_SECURE", "auto")
    r = await panel_client.post(
        "/login",
        data={"username": ADMIN_USERNAME, "password": ADMIN_PASSWORD},
        follow_redirects=False,
    )
    assert r.status_code == 302
    set_cookie = r.headers.get("set-cookie", "")
    assert "dc_session=" in set_cookie
    assert "secure" not in set_cookie.lower()

    # The follow-up request with that cookie reaches the dashboard (no loop).
    r = await panel_client.get("/", follow_redirects=False)
    assert r.status_code == 200


async def test_cookie_secure_forced_true_override(panel_client, monkeypatch) -> None:
    from app.config import settings

    monkeypatch.setattr(settings, "COOKIE_SECURE", "true")
    r = await panel_client.post(
        "/login",
        data={"username": ADMIN_USERNAME, "password": ADMIN_PASSWORD},
        follow_redirects=False,
    )
    assert r.status_code == 302
    set_cookie = r.headers.get("set-cookie", "")
    assert "dc_session=" in set_cookie
    assert "secure" in set_cookie.lower()


async def test_form_login_by_email_identifier_also_works(panel_client) -> None:
    r = await panel_client.post(
        "/login",
        data={"username": ADMIN_EMAIL, "password": ADMIN_PASSWORD},
        follow_redirects=False,
    )
    assert r.status_code == 302


async def test_form_login_wrong_password_rejected(panel_client) -> None:
    panel_client.cookies.set("dc_lang", "en")
    r = await panel_client.post(
        "/login", data={"username": ADMIN_USERNAME, "password": "wrong"}
    )
    assert r.status_code == 401
    assert "Invalid" in r.text


async def test_api_settings_fresh_install_shows_catalog_defaults(panel_client) -> None:
    await _login(panel_client)
    r = await panel_client.get("/api/settings")
    assert r.status_code == 200
    values = {
        item["key"]: item["value"]
        for cat in r.json()["categories"]
        for item in cat["items"]
    }
    # Catalog defaults are visible even though no row has been saved yet.
    assert values["sales_enabled"] is True
    assert values["low_stock_threshold"] == 5
    assert values["card_number"] == ""


async def test_settings_form_roundtrip_and_typed_api(panel_client) -> None:
    await _login(panel_client)

    # Save via the HTML form; unchecked checkboxes mean False.
    r = await panel_client.post(
        "/settings",
        data={"card_number": "6037-1234", "min_wallet_topup": "5000"},
        follow_redirects=False,
    )
    assert r.status_code == 303 and "saved=1" in r.headers["location"]

    r = await panel_client.get("/settings")
    assert r.status_code == 200 and "6037-1234" in r.text

    r = await panel_client.get("/api/settings")
    values = {
        item["key"]: item["value"]
        for cat in r.json()["categories"]
        for item in cat["items"]
    }
    assert values["card_number"] == "6037-1234"
    assert values["min_wallet_topup"] == 5000
    assert values["wallet_enabled"] is False  # checkbox omitted from the form


async def test_api_settings_unknown_key_rejected(panel_client) -> None:
    await _login(panel_client)
    r = await panel_client.put("/api/settings", json={"values": {"bogus": 1}})
    assert r.status_code == 400

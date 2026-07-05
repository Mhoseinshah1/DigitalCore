"""Web settings RBAC (Phase 2).

manage_settings gates general/telegram/bot-texts; payment is view_payments to
view and manage_payments to save. Accountant can view payment settings but not
change them.
"""
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

PASSWORD = "rbac-pass-1"

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
        username = f"rbac_{role}"
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


async def test_viewer_cannot_view_or_save_settings(client_with_role) -> None:
    client = await client_with_role("viewer")
    r = await client.get("/admin/settings/general")
    assert r.status_code == 403
    r = await client.post(
        "/admin/settings/payment", data={"card_number": "1111"}, follow_redirects=False
    )
    assert r.status_code == 403
    r = await client.put("/api/settings", json={"values": {"card_number": "1111"}})
    assert r.status_code == 403


async def test_support_cannot_save_settings(client_with_role) -> None:
    client = await client_with_role("support")
    r = await client.post(
        "/admin/settings/general", data={"site_name": "X"}, follow_redirects=False
    )
    assert r.status_code == 403


async def test_accountant_can_view_but_not_save_payment(client_with_role) -> None:
    client = await client_with_role("accountant")
    # view_payments lets an accountant open the payment page...
    r = await client.get("/admin/settings/payment")
    assert r.status_code == 200
    # ...but manage_payments is required to save it.
    r = await client.post(
        "/admin/settings/payment", data={"card_number": "9999"}, follow_redirects=False
    )
    assert r.status_code == 403
    # And they can't open the general (manage_settings) page.
    r = await client.get("/admin/settings/general")
    assert r.status_code == 403


async def test_admin_can_view_and_save_settings(client_with_role) -> None:
    client = await client_with_role("admin")
    r = await client.get("/admin/settings/general")
    assert r.status_code == 200
    r = await client.post(
        "/admin/settings/payment", data={"card_number": "3333-4444"}, follow_redirects=False
    )
    assert r.status_code == 303 and "saved=1" in r.headers["location"]
    r = await client.get("/api/settings")
    values = {
        i["key"]: i["value"] for cat in r.json()["categories"] for i in cat["items"]
    }
    assert values["card_number"] == "3333-4444"


async def test_owner_can_save_settings(client_with_role) -> None:
    client = await client_with_role("owner")
    r = await client.put("/api/settings", json={"values": {"sheba_number": "IR123"}})
    assert r.status_code == 200
    r = await client.put(
        "/api/settings", json={"values": {"min_wallet_topup": "garbage"}}
    )
    assert r.status_code == 400  # type validation surfaces as 400

"""Web 3X-UI servers RBAC: only manage_xui roles (owner/admin) may view/manage."""
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

PASSWORD = "srv-rbac-1"

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
        username = f"srvrbac_{role}"
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


VALID_FORM = {
    "name": "RBAC Server",
    "base_url": "http://panel.example:2053",
    "panel_version": "2.9.4",
    "username": "admin",
    "password": "panel-pw",
}


async def test_viewer_cannot_view_or_create_servers(client_with_role) -> None:
    client = await client_with_role("viewer")
    r = await client.get("/admin/servers")
    assert r.status_code == 403
    r = await client.post("/admin/servers/new", data=VALID_FORM, follow_redirects=False)
    assert r.status_code == 403


async def test_accountant_cannot_manage_servers(client_with_role) -> None:
    client = await client_with_role("accountant")
    r = await client.post("/admin/servers/new", data=VALID_FORM, follow_redirects=False)
    assert r.status_code == 403


async def test_admin_can_create_and_list_servers(client_with_role) -> None:
    client = await client_with_role("admin")
    r = await client.get("/admin/servers")
    assert r.status_code == 200

    r = await client.post("/admin/servers/new", data=VALID_FORM, follow_redirects=False)
    assert r.status_code == 303 and "saved=1" in r.headers["location"]

    r = await client.get("/admin/servers")
    assert "RBAC Server" in r.text
    # The chosen panel version is rendered verbatim (language-independent).
    assert "2.9.4" in r.text

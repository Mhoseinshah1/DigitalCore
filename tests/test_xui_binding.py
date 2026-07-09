"""Phase 2.1: XUI server/inbound foundation — service, web routes, JSON, security.

Covers credential handling (encrypt at rest, keep-on-empty), inbound CRUD and
active filtering, RBAC on the /admin/xui-* routes, the product-form JSON
endpoint, and the guarantee that credentials never leak into rendered pages or
audit metadata.
"""
from __future__ import annotations

from collections.abc import AsyncIterator, Awaitable, Callable

import httpx
import pytest_asyncio
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker, create_async_engine
from sqlalchemy.pool import StaticPool

from app.core import crypto
from app.core.security import hash_password
from app.database import get_session
from app.models import Admin, AuditLog, Base
from app.services import xui_server_service
from app.web.main import app

PASSWORD = "xui-rbac-1"

ClientFactory = Callable[[str], Awaitable[httpx.AsyncClient]]


# --------------------------------------------------------------------------
# Service-level tests (db_session fixture from conftest)
# --------------------------------------------------------------------------
async def test_create_server_encrypts_credentials(db_session) -> None:
    server = await xui_server_service.create_server(
        db_session, name="Panel A", base_url="http://1.2.3.4:2053/",
        username="root", password="s3cret", api_token="tok-123",
    )
    await db_session.commit()
    # Trailing slash trimmed; credentials stored as marked ciphertext, not plaintext.
    assert server.base_url == "http://1.2.3.4:2053"
    assert server.encrypted_password and server.encrypted_password.startswith("enc::")
    assert "s3cret" not in server.encrypted_password
    assert crypto.decrypt(server.encrypted_password) == "s3cret"
    assert crypto.decrypt(server.encrypted_api_token) == "tok-123"


async def test_update_server_keeps_password_when_blank(db_session) -> None:
    server = await xui_server_service.create_server(
        db_session, name="Panel B", base_url="http://p:2053", password="orig-pw",
    )
    await db_session.commit()
    original_cipher = server.encrypted_password

    # Empty password on edit → keep the stored one; other fields still change.
    await xui_server_service.update_server(
        db_session, server.id, name="Panel B2", password="", api_token="",
    )
    await db_session.commit()
    assert server.name == "Panel B2"
    assert server.encrypted_password == original_cipher
    assert crypto.decrypt(server.encrypted_password) == "orig-pw"

    # A non-empty password replaces it.
    await xui_server_service.update_server(db_session, server.id, password="new-pw")
    await db_session.commit()
    assert crypto.decrypt(server.encrypted_password) == "new-pw"


async def test_deactivate_server_is_soft(db_session) -> None:
    server = await xui_server_service.create_server(
        db_session, name="Panel C", base_url="http://p:2053",
    )
    await db_session.commit()
    await xui_server_service.delete_or_deactivate_server(db_session, server.id)
    await db_session.commit()
    assert server.is_active is False and server.status == "inactive"
    # Still retrievable (not destroyed) so product bindings survive.
    assert await xui_server_service.get_server(db_session, server.id) is not None


async def test_list_servers_active_only(db_session) -> None:
    a = await xui_server_service.create_server(db_session, name="A", base_url="http://a")
    b = await xui_server_service.create_server(db_session, name="B", base_url="http://b")
    await db_session.commit()
    await xui_server_service.delete_or_deactivate_server(db_session, b.id)
    await db_session.commit()
    all_ids = {s.id for s in await xui_server_service.list_servers(db_session)}
    active_ids = {
        s.id for s in await xui_server_service.list_servers(db_session, active_only=True)
    }
    assert all_ids == {a.id, b.id}
    assert active_ids == {a.id}


async def test_inbound_crud_and_active_filter(db_session) -> None:
    server = await xui_server_service.create_server(db_session, name="S", base_url="http://s")
    await db_session.commit()
    ib1 = await xui_server_service.create_inbound(
        db_session, server.id, 10, remark="one", protocol="vless", port=443,
        network="tcp", security="reality",
    )
    ib2 = await xui_server_service.create_inbound(db_session, server.id, 20, remark="two")
    await db_session.commit()

    await xui_server_service.deactivate_inbound(db_session, ib2.id)
    await db_session.commit()

    all_ib = await xui_server_service.list_inbounds(db_session, server.id)
    active_ib = await xui_server_service.list_inbounds(db_session, server.id, active_only=True)
    assert {i.id for i in all_ib} == {ib1.id, ib2.id}
    assert {i.id for i in active_ib} == {ib1.id}

    counts = await xui_server_service.inbound_counts(db_session)
    assert counts.get(server.id) == 2


async def test_audit_metadata_has_no_credentials(db_session) -> None:
    server = await xui_server_service.create_server(
        db_session, name="Secretful", base_url="http://p:2053",
        username="root", password="TOP-SECRET-PW", api_token="TOP-SECRET-TOKEN",
        actor_id=1,
    )
    await xui_server_service.update_server(
        db_session, server.id, password="ANOTHER-SECRET", actor_id=1
    )
    await db_session.commit()
    rows = (await db_session.execute(select(AuditLog))).scalars().all()
    assert rows  # something was logged
    blob = " ".join(
        f"{r.action} {r.old_value} {r.new_value} {r.meta}" for r in rows
    )
    for secret in ("TOP-SECRET-PW", "TOP-SECRET-TOKEN", "ANOTHER-SECRET"):
        assert secret not in blob


# --------------------------------------------------------------------------
# Web-layer tests (dedicated in-memory app + login)
# --------------------------------------------------------------------------
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
        username = f"xuirbac_{role}"
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


SERVER_FORM = {
    "name": "Web Panel",
    "base_url": "http://panel.example:2053",
    "username": "root",
    "password": "web-secret-pw",
    "api_token": "web-secret-token",
    "is_active": "on",
}


async def test_viewer_and_accountant_cannot_manage_xui(client_with_role) -> None:
    for role in ("viewer", "accountant", "support"):
        client = await client_with_role(role)
        assert (await client.get("/admin/xui-servers")).status_code == 403
        r = await client.post(
            "/admin/xui-servers/create", data=SERVER_FORM, follow_redirects=False
        )
        assert r.status_code == 403


async def test_admin_full_server_and_inbound_flow(client_with_role) -> None:
    client = await client_with_role("admin")

    # Create a server.
    r = await client.post(
        "/admin/xui-servers/create", data=SERVER_FORM, follow_redirects=False
    )
    assert r.status_code == 303 and "saved=1" in r.headers["location"]

    # List shows it without leaking credentials.
    r = await client.get("/admin/xui-servers")
    assert r.status_code == 200
    assert "Web Panel" in r.text
    assert "web-secret-pw" not in r.text
    assert "web-secret-token" not in r.text

    # Edit form never renders the stored password/token.
    r = await client.get("/admin/xui-servers/1/edit")
    assert r.status_code == 200
    assert "web-secret-pw" not in r.text
    assert "web-secret-token" not in r.text

    # Editing with an empty password keeps the stored one (round-trips fine).
    r = await client.post(
        "/admin/xui-servers/1/edit",
        data={"name": "Web Panel 2", "base_url": "http://panel.example:2053",
              "username": "root", "password": "", "api_token": "", "is_active": "on"},
        follow_redirects=False,
    )
    assert r.status_code == 303 and "saved=1" in r.headers["location"]

    # Add an inbound.
    r = await client.post(
        "/admin/xui-servers/1/inbounds/create",
        data={"inbound_id": "7", "remark": "primary", "protocol": "vless",
              "port": "443", "network": "tcp", "security": "reality", "is_active": "on"},
        follow_redirects=False,
    )
    assert r.status_code == 303 and "saved=1" in r.headers["location"]

    r = await client.get("/admin/xui-servers/1/inbounds")
    assert r.status_code == 200 and "primary" in r.text
    # The inbounds page no longer offers manual add/edit — only sync + local toggle.
    assert "/inbounds/create" not in r.text          # no «add inbound» affordance
    assert "/xui-inbounds/1/edit" not in r.text       # no manual edit link
    assert "sync-inbounds" in r.text                  # «Sync from panel» button present

    # The manual create form page (GET) is gone (only the programmatic POST remains).
    assert (await client.get("/admin/xui-servers/1/inbounds/create")).status_code in (404, 405)
    # The manual edit form/route is gone entirely.
    assert (await client.get("/admin/xui-inbounds/1/edit")).status_code == 404
    assert (await client.post("/admin/xui-inbounds/1/edit", follow_redirects=False)).status_code == 404

    # A locally-disabled inbound can be re-enabled for sale (no remote change).
    r = await client.post("/admin/xui-inbounds/1/deactivate", follow_redirects=False)
    assert r.status_code == 303 and "saved=1" in r.headers["location"]
    r = await client.post("/admin/xui-inbounds/1/activate", follow_redirects=False)
    assert r.status_code == 303 and "saved=1" in r.headers["location"]

    # Deactivate the server (soft).
    r = await client.post("/admin/xui-servers/1/deactivate", follow_redirects=False)
    assert r.status_code == 303 and "saved=1" in r.headers["location"]


async def test_json_inbounds_active_only_and_permission(client_with_role) -> None:
    admin = await client_with_role("admin")
    await admin.post("/admin/xui-servers/create", data=SERVER_FORM, follow_redirects=False)
    # Two inbounds: one active, one that we deactivate.
    await admin.post(
        "/admin/xui-servers/1/inbounds/create",
        data={"inbound_id": "1", "remark": "act", "is_active": "on"},
        follow_redirects=False,
    )
    await admin.post(
        "/admin/xui-servers/1/inbounds/create",
        data={"inbound_id": "2", "remark": "inact", "is_active": "on"},
        follow_redirects=False,
    )
    # Deactivate inbound record #2.
    await admin.post("/admin/xui-inbounds/2/deactivate", follow_redirects=False)

    r = await admin.get("/admin/api/xui-servers/1/inbounds")
    assert r.status_code == 200
    remarks = {ib["remark"] for ib in r.json()["inbounds"]}
    assert remarks == {"act"}  # inactive inbound is filtered out

    # A role without manage_products is forbidden from the JSON endpoint.
    support = await client_with_role("support")
    r = await support.get("/admin/api/xui-servers/1/inbounds")
    assert r.status_code == 403


async def test_anonymous_json_is_unauthorized(client_with_role) -> None:
    # Fresh client, no login.
    transport = httpx.ASGITransport(app=app)
    async with httpx.AsyncClient(transport=transport, base_url="http://testserver") as c:
        r = await c.get("/admin/api/xui-servers/1/inbounds")
        assert r.status_code == 401

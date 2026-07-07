"""Phase 9 web: ticket + tutorial RBAC and end-to-end reply/attachment flows."""
from __future__ import annotations

import tempfile
from collections.abc import AsyncIterator, Awaitable, Callable
from pathlib import Path

import httpx
import pytest_asyncio
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker, create_async_engine
from sqlalchemy.pool import StaticPool

import app.services.ticket_service as ticket_service
from app.core.security import hash_password
from app.database import get_session
from app.models import Admin, Base, User
from app.web.main import app

PASSWORD = "tk-rbac-1"
ClientFactory = Callable[[str], Awaitable[httpx.AsyncClient]]


@pytest_asyncio.fixture
async def env(monkeypatch) -> AsyncIterator[tuple[ClientFactory, async_sessionmaker]]:
    monkeypatch.setattr(ticket_service, "TICKETS_ROOT", Path(tempfile.mkdtemp()) / "tickets")
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
        username = f"tkrbac_{role}"
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
        yield factory, maker
    finally:
        for c in clients:
            await c.aclose()
        app.dependency_overrides.pop(get_session, None)
        await engine.dispose()


async def _seed_ticket(maker) -> int:
    async with maker() as s:
        u = User(telegram_id=555, first_name="U", language="fa")
        s.add(u)
        await s.flush()
        tk = await ticket_service.create_ticket(s, u.id, "Help me", "it broke")
        await s.commit()
        return tk.id


async def test_tickets_require_auth(env) -> None:
    factory, _ = env
    transport = httpx.ASGITransport(app=app)
    async with httpx.AsyncClient(transport=transport, base_url="http://testserver") as anon:
        r = await anon.get("/admin/tickets", follow_redirects=False)
        assert r.status_code in (302, 307) and "/admin/login" in r.headers.get("location", "")


async def test_viewer_can_view_but_not_reply(env) -> None:
    factory, maker = env
    ticket_id = await _seed_ticket(maker)
    client = await factory("viewer")
    r = await client.get("/admin/tickets")
    assert r.status_code == 200
    r = await client.get(f"/admin/tickets/{ticket_id}")
    assert r.status_code == 200
    # Viewer lacks manage_tickets → reply is forbidden.
    r = await client.post(f"/admin/tickets/{ticket_id}/reply", data={"message": "hi"},
                          follow_redirects=False)
    assert r.status_code == 403


async def test_support_can_reply_and_close(env) -> None:
    factory, maker = env
    ticket_id = await _seed_ticket(maker)
    client = await factory("support")
    r = await client.post(f"/admin/tickets/{ticket_id}/reply", data={"message": "on it"},
                          follow_redirects=False)
    assert r.status_code == 303 and "saved=replied" in r.headers["location"]
    r = await client.post(f"/admin/tickets/{ticket_id}/priority", data={"priority": "high"},
                          follow_redirects=False)
    assert r.status_code == 303
    r = await client.post(f"/admin/tickets/{ticket_id}/assign", follow_redirects=False)
    assert r.status_code == 303
    r = await client.post(f"/admin/tickets/{ticket_id}/close", follow_redirects=False)
    assert r.status_code == 303 and "saved=closed" in r.headers["location"]
    # The reply landed in the thread.
    async with maker() as s:
        tk = await ticket_service.get_ticket(s, ticket_id)
        assert any(m.sender_type == "admin" for m in tk.messages)
        assert tk.status == "closed" and tk.priority == "high"


async def test_reply_with_attachment(env) -> None:
    factory, maker = env
    ticket_id = await _seed_ticket(maker)
    client = await factory("owner")
    files = {"attachment": ("note.png", b"\x89PNG data", "image/png")}
    r = await client.post(f"/admin/tickets/{ticket_id}/reply",
                          data={"message": "see attached"}, files=files,
                          follow_redirects=False)
    assert r.status_code == 303
    async with maker() as s:
        tk = await ticket_service.get_ticket(s, ticket_id)
        admin_msg = [m for m in tk.messages if m.sender_type == "admin"][-1]
        assert admin_msg.attachment_path
        msg_id = admin_msg.id
    # The attachment is served to an authenticated admin.
    r = await client.get(f"/admin/tickets/attachments/{msg_id}")
    assert r.status_code == 200 and r.content == b"\x89PNG data"


# --- tutorials RBAC ---------------------------------------------------------
async def test_tutorials_require_manage_permission(env) -> None:
    factory, _ = env
    # Support has view_tickets but NOT manage_tutorials.
    support = await factory("support")
    r = await support.get("/admin/tutorials")
    assert r.status_code == 403
    r = await support.post("/admin/tutorials/create",
                           data={"title": "x", "content": "y", "type": "v2ray"},
                           follow_redirects=False)
    assert r.status_code == 403


async def test_admin_tutorial_and_category_crud(env) -> None:
    factory, maker = env
    client = await factory("admin")
    # Create a category.
    r = await client.post("/admin/tutorial-categories/create",
                          data={"title": "Guides", "sort_order": "1", "is_active": "on"},
                          follow_redirects=False)
    assert r.status_code == 303 and "saved=created" in r.headers["location"]
    # Create a tutorial.
    r = await client.post("/admin/tutorials/create",
                          data={"title": "Android setup", "content": "step1\nstep2",
                                "platform": "android", "product_type": "v2ray",
                                "is_active": "on"},
                          follow_redirects=False)
    assert r.status_code == 303 and "saved=created" in r.headers["location"]
    r = await client.get("/admin/tutorials")
    assert "Android setup" in r.text

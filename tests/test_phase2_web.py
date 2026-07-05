"""Phase 2 web panel: auth gates + user block/wallet mutations write audit rows."""
from __future__ import annotations

from collections.abc import AsyncIterator

import httpx
import pytest
import pytest_asyncio
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker, create_async_engine
from sqlalchemy.pool import StaticPool

from app.core.security import hash_password
from app.database import get_session
from app.models import Admin, AuditLog, Base, User
from app.web.main import app

PASSWORD = "p2web-pass"


@pytest_asyncio.fixture
async def panel() -> AsyncIterator[tuple[httpx.AsyncClient, object]]:
    """Logged-in owner client + the session maker (seeded with one bot user)."""
    engine = create_async_engine(
        "sqlite+aiosqlite://", poolclass=StaticPool,
        connect_args={"check_same_thread": False},
    )
    async with engine.begin() as conn:
        await conn.run_sync(Base.metadata.create_all)
    maker = async_sessionmaker(engine, class_=AsyncSession, expire_on_commit=False)
    async with maker() as s:
        s.add(Admin(username="owner", password_hash=hash_password(PASSWORD),
                    is_active=True, is_super_admin=True, role="owner"))
        s.add(User(telegram_id=999001, username="bob", first_name="Bob"))
        await s.commit()

    async def _override():
        async with maker() as session:
            yield session

    app.dependency_overrides[get_session] = _override
    transport = httpx.ASGITransport(app=app)
    client = httpx.AsyncClient(transport=transport, base_url="http://testserver")
    r = await client.post("/admin/login", data={"username": "owner", "password": PASSWORD},
                          follow_redirects=False)
    assert r.status_code == 302
    try:
        yield client, maker
    finally:
        await client.aclose()
        app.dependency_overrides.pop(get_session, None)
        await engine.dispose()


PROTECTED = ["/admin", "/admin/users", "/admin/users/blocked", "/admin/products",
             "/admin/settings/general", "/admin/settings/payment", "/admin/audit-logs"]


@pytest.mark.parametrize("path", PROTECTED)
async def test_protected_pages_redirect_anonymous(path) -> None:
    transport = httpx.ASGITransport(app=app)
    async with httpx.AsyncClient(transport=transport, base_url="http://testserver") as c:
        r = await c.get(path, follow_redirects=False)
    assert r.status_code == 302
    assert r.headers["location"] == "/admin/login"


async def test_users_page_lists_and_detail_renders(panel) -> None:
    client, maker = panel
    r = await client.get("/admin/users")
    assert r.status_code == 200 and "bob" in r.text
    async with maker() as s:
        uid = (await s.execute(select(User.id))).scalar_one()
    r = await client.get(f"/admin/users/{uid}")
    assert r.status_code == 200 and "999001" in r.text


async def test_block_and_wallet_adjust_via_web_are_audited(panel) -> None:
    client, maker = panel
    async with maker() as s:
        uid = (await s.execute(select(User.id))).scalar_one()

    r = await client.post(f"/admin/users/{uid}/block", follow_redirects=False)
    assert r.status_code == 303
    r = await client.post(f"/admin/users/{uid}/wallet-adjust",
                          data={"direction": "add", "amount": "12000", "reason": "gift"},
                          follow_redirects=False)
    assert r.status_code == 303 and "saved=1" in r.headers["location"]

    async with maker() as s:
        user = await s.get(User, uid)
        assert user.is_blocked is True and user.wallet_balance == 12000
        actions = {r.action for r in (await s.execute(select(AuditLog))).scalars().all()}
    assert {"admin.login", "user.blocked", "wallet.adjusted"} <= actions

    # The audit-logs page renders the actions.
    r = await client.get("/admin/audit-logs")
    assert r.status_code == 200 and "wallet.adjusted" in r.text


async def test_wallet_adjust_negative_guard_surfaces_error(panel) -> None:
    client, maker = panel
    async with maker() as s:
        uid = (await s.execute(select(User.id))).scalar_one()
    r = await client.post(f"/admin/users/{uid}/wallet-adjust",
                          data={"direction": "subtract", "amount": "5000"},
                          follow_redirects=False)
    assert r.status_code == 303 and "error=" in r.headers["location"]
    async with maker() as s:
        assert (await s.get(User, uid)).wallet_balance == 0

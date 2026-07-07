"""Phase 10 web: coupon + referral-reward RBAC and CRUD flows."""
from __future__ import annotations

from collections.abc import AsyncIterator, Awaitable, Callable

import httpx
import pytest_asyncio
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker, create_async_engine
from sqlalchemy.pool import StaticPool

from app.core.security import hash_password
from app.database import get_session
from app.models import Admin, Base, Order, ReferralReward, User
from app.web.main import app

PASSWORD = "mkt-rbac-1"
ClientFactory = Callable[[str], Awaitable[httpx.AsyncClient]]


@pytest_asyncio.fixture
async def env() -> AsyncIterator[tuple[ClientFactory, async_sessionmaker]]:
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
        username = f"mkt_{role}"
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


async def test_coupons_require_auth(env) -> None:
    transport = httpx.ASGITransport(app=app)
    async with httpx.AsyncClient(transport=transport, base_url="http://testserver") as anon:
        r = await anon.get("/admin/coupons", follow_redirects=False)
        assert r.status_code in (302, 307) and "/admin/login" in r.headers.get("location", "")
        r = await anon.get("/admin/referrals", follow_redirects=False)
        assert r.status_code in (302, 307)


async def test_viewer_can_view_but_not_manage_coupons(env) -> None:
    factory, _ = env
    client = await factory("viewer")
    assert (await client.get("/admin/coupons")).status_code == 200
    # Viewer lacks manage_coupons.
    r = await client.post("/admin/coupons/create",
                          data={"code": "X", "discount_type": "percent", "discount_value": "10"},
                          follow_redirects=False)
    assert r.status_code == 403
    # Viewer lacks manage_referrals.
    assert (await client.get("/admin/referrals", follow_redirects=False)).status_code == 403


async def test_admin_coupon_crud(env) -> None:
    factory, maker = env
    client = await factory("admin")
    r = await client.post("/admin/coupons/create",
                          data={"code": "save20", "discount_type": "percent",
                                "discount_value": "20", "is_active": "on"},
                          follow_redirects=False)
    assert r.status_code == 303 and "saved=created" in r.headers["location"]
    r = await client.get("/admin/coupons")
    assert "SAVE20" in r.text  # normalized to uppercase
    # Reject a bad percent value via the form handler.
    r = await client.post("/admin/coupons/create",
                          data={"code": "BAD", "discount_type": "percent", "discount_value": "0"},
                          follow_redirects=False)
    assert r.status_code == 303 and "error=" in r.headers["location"]
    # Usages page loads.
    async with maker() as s:
        from app.services import coupon_service
        cid = (await coupon_service.get_by_code(s, "SAVE20")).id
    assert (await client.get(f"/admin/coupons/{cid}/usages")).status_code == 200


async def test_reward_approve_pay_permission(env) -> None:
    factory, maker = env
    # Seed a pending reward.
    async with maker() as s:
        ref = User(telegram_id=1, first_name="R", wallet_balance=0)
        buyer = User(telegram_id=2, first_name="B")
        s.add_all([ref, buyer])
        await s.flush()
        reward = ReferralReward(referrer_user_id=ref.id, referred_user_id=buyer.id,
                                reward_type="fixed", reward_amount=5000, status="pending")
        s.add(reward)
        await s.commit()
        rid, ref_id = reward.id, ref.id

    # Support lacks manage_referrals.
    support = await factory("support")
    r = await support.post(f"/admin/referral-rewards/{rid}/approve", follow_redirects=False)
    assert r.status_code == 403

    # Owner approves → paid + wallet credited.
    owner = await factory("owner")
    r = await owner.post(f"/admin/referral-rewards/{rid}/approve", follow_redirects=False)
    assert r.status_code == 303 and "saved=approved" in r.headers["location"]
    async with maker() as s:
        assert (await s.get(User, ref_id)).wallet_balance == 5000
        assert (await s.get(ReferralReward, rid)).status == "paid"


async def test_accountant_can_manage_rewards(env) -> None:
    factory, _ = env
    client = await factory("accountant")
    # Accountant has manage_referrals (wallet payouts).
    assert (await client.get("/admin/referral-rewards")).status_code == 200
    # Accountant has view_coupons but NOT manage_coupons.
    assert (await client.get("/admin/coupons")).status_code == 200
    r = await client.post("/admin/coupons/create",
                          data={"code": "N", "discount_type": "fixed", "discount_value": "100"},
                          follow_redirects=False)
    assert r.status_code == 403

"""Phase 7 web: wallet top-up pages auth/permission, approve/reject, receipts."""
from __future__ import annotations

from collections.abc import AsyncIterator

import httpx
import pytest_asyncio
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker, create_async_engine
from sqlalchemy.pool import StaticPool

from app.core.security import hash_password
from app.database import get_session
from app.models import Admin, Base, User, WalletTopupRequest
from app.services import wallet_service
from app.web.main import app

PW = "wallet-web-1"


@pytest_asyncio.fixture
async def env(monkeypatch) -> AsyncIterator[dict]:
    engine = create_async_engine("sqlite+aiosqlite://", poolclass=StaticPool,
                                 connect_args={"check_same_thread": False})
    async with engine.begin() as conn:
        await conn.run_sync(Base.metadata.create_all)
    maker = async_sessionmaker(engine, class_=AsyncSession, expire_on_commit=False)

    async def _override():
        async with maker() as s:
            yield s

    app.dependency_overrides[get_session] = _override
    transport = httpx.ASGITransport(app=app)
    clients: list[httpx.AsyncClient] = []

    async def login(role: str) -> httpx.AsyncClient:
        async with maker() as s:
            s.add(Admin(username=f"ww_{role}", password_hash=hash_password(PW),
                        is_active=True, is_super_admin=(role == "owner"), role=role))
            await s.commit()
        c = httpx.AsyncClient(transport=transport, base_url="http://t")
        clients.append(c)
        r = await c.post("/admin/login", data={"username": f"ww_{role}", "password": PW},
                         follow_redirects=False)
        assert r.status_code == 302
        return c

    async def make_topup(status: str = "waiting_admin", amount: int = 20_000) -> tuple[int, int]:
        async with maker() as s:
            u = User(telegram_id=1, first_name="B", language="fa", wallet_balance=0)
            s.add(u)
            await s.flush()
            t = WalletTopupRequest(user_id=u.id, amount=amount, status=status)
            s.add(t)
            await s.commit()
            return u.id, t.id

    try:
        yield {"maker": maker, "login": login, "make_topup": make_topup, "transport": transport}
    finally:
        for c in clients:
            await c.aclose()
        app.dependency_overrides.pop(get_session, None)
        await engine.dispose()


async def test_topups_requires_auth(env) -> None:
    async with httpx.AsyncClient(transport=env["transport"], base_url="http://t") as c:
        for path in ("/admin/wallet/topups", "/admin/wallet/topups/pending",
                     "/admin/wallet/transactions"):
            r = await c.get(path, follow_redirects=False)
            assert r.status_code == 302


async def test_pending_and_transactions_open(env) -> None:
    await env["make_topup"]()
    admin = await env["login"]("admin")
    assert (await admin.get("/admin/wallet/topups/pending")).status_code == 200
    assert (await admin.get("/admin/wallet/transactions")).status_code == 200
    assert (await admin.get("/admin/wallet/topups")).status_code == 200


async def test_approve_requires_permission(env) -> None:
    _uid, tid = await env["make_topup"]()
    support = await env["login"]("support")  # view_wallet_topups but not manage
    r = await support.post(f"/admin/wallet/topups/{tid}/approve", follow_redirects=False)
    assert r.status_code == 403


async def test_approve_credits_balance(env) -> None:
    uid, tid = await env["make_topup"](amount=15_000)
    admin = await env["login"]("admin")
    r = await admin.post(f"/admin/wallet/topups/{tid}/approve", follow_redirects=False)
    assert r.status_code == 303 and "saved=approved" in r.headers["location"]
    async with env["maker"]() as s:
        assert await wallet_service.get_balance(s, uid) == 15_000
        t = await s.get(WalletTopupRequest, tid)
        assert t.status == "approved"


async def test_reject_reason_required(env) -> None:
    uid, tid = await env["make_topup"]()
    admin = await env["login"]("admin")
    r = await admin.post(f"/admin/wallet/topups/{tid}/reject", data={"reason": "  "},
                         follow_redirects=False)
    assert r.status_code == 303 and "error=" in r.headers["location"]
    async with env["maker"]() as s:
        assert await wallet_service.get_balance(s, uid) == 0  # not credited
        t = await s.get(WalletTopupRequest, tid)
        assert t.status == "waiting_admin"  # unchanged


async def test_reject_with_reason(env) -> None:
    uid, tid = await env["make_topup"]()
    admin = await env["login"]("admin")
    r = await admin.post(f"/admin/wallet/topups/{tid}/reject", data={"reason": "blurry"},
                         follow_redirects=False)
    assert "saved=rejected" in r.headers["location"]
    async with env["maker"]() as s:
        t = await s.get(WalletTopupRequest, tid)
        assert t.status == "rejected" and t.reject_reason == "blurry"


async def test_receipt_access_protected(env) -> None:
    _uid, tid = await env["make_topup"]()
    # Unauthenticated → redirect to login.
    async with httpx.AsyncClient(transport=env["transport"], base_url="http://t") as c:
        r = await c.get(f"/admin/wallet/receipts/{tid}", follow_redirects=False)
        assert r.status_code == 302
    # Authenticated but the top-up has no receipt file → 404, never a traversal.
    admin = await env["login"]("admin")
    r = await admin.get(f"/admin/wallet/receipts/{tid}")
    assert r.status_code == 404


async def test_refund_requires_permission(env) -> None:
    # support lacks refund_payments.
    async with env["maker"]() as s:
        from app.models import Order, Product
        u = User(telegram_id=2, first_name="B", wallet_balance=0)
        p = Product(type="license", title="P", price=1000, is_active=True, is_hidden=False)
        s.add_all([u, p])
        await s.flush()
        o = Order(order_number="DC-R-1", user_id=u.id, product_id=p.id, amount=1000,
                  final_amount=1000, status="delivered", payment_method="wallet")
        s.add(o)
        await s.commit()
        order_id = o.id
    support = await env["login"]("support")
    r = await support.post(f"/admin/orders/{order_id}/refund", data={"reason": "x"},
                           follow_redirects=False)
    assert r.status_code == 403

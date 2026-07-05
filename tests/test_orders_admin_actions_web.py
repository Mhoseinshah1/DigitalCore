"""Web receipt-review quick actions: auth, permission gating, wallet, block/restrict."""
from __future__ import annotations

from collections.abc import AsyncIterator

import httpx
import pytest_asyncio
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker, create_async_engine
from sqlalchemy.pool import StaticPool

from app.core.security import hash_password
from app.database import get_session
from app.models import Admin, AuditLog, Base, Product, User, WalletTransaction
from app.services import license_service, order_service, payment_service, user_service
from app.services.payment_service import ReceiptFile
from app.web.main import app

PW = "p4-web-1"


@pytest_asyncio.fixture
async def env(monkeypatch, tmp_path) -> AsyncIterator[dict]:
    engine = create_async_engine("sqlite+aiosqlite://", poolclass=StaticPool,
                                 connect_args={"check_same_thread": False})
    async with engine.begin() as conn:
        await conn.run_sync(Base.metadata.create_all)
    maker = async_sessionmaker(engine, class_=AsyncSession, expire_on_commit=False)
    monkeypatch.setattr(payment_service, "RECEIPTS_ROOT", tmp_path)

    async def _override():
        async with maker() as s:
            yield s

    app.dependency_overrides[get_session] = _override
    transport = httpx.ASGITransport(app=app)
    clients: list[httpx.AsyncClient] = []

    async def login(role: str) -> httpx.AsyncClient:
        async with maker() as s:
            s.add(Admin(username=f"u_{role}", password_hash=hash_password(PW),
                        is_active=True, is_super_admin=(role == "owner"), role=role))
            await s.commit()
        c = httpx.AsyncClient(transport=transport, base_url="http://t")
        clients.append(c)
        r = await c.post("/admin/login", data={"username": f"u_{role}", "password": PW},
                         follow_redirects=False)
        assert r.status_code == 302
        return c

    async def seed() -> tuple[int, int]:
        async with maker() as s:
            u = User(telegram_id=5, username="bob", first_name="B", wallet_balance=0)
            s.add(u)
            p = Product(type="license", title="Key", price=50000, is_active=True, is_hidden=False)
            s.add(p)
            await s.flush()
            await license_service.add_keys(s, p.id, ["LIC-1"], actor_id=1)
            order = await order_service.create_order(s, u.id, p.id)
            await payment_service.create_payment_for_order(s, order)
            fi = ReceiptFile(content=b"\x89PNG\r\n\x1a\n" + b"x" * 20,
                             original_name="r.png", mime_type="image/png", file_id="f")
            await payment_service.submit_receipt(s, order.id, u.id, fi)
            await s.commit()
            return order.id, u.id

    try:
        yield {"maker": maker, "login": login, "seed": seed, "transport": transport}
    finally:
        for c in clients:
            await c.aclose()
        app.dependency_overrides.pop(get_session, None)
        await engine.dispose()


async def test_approve_requires_auth(env) -> None:
    oid, _uid = await env["seed"]()
    async with httpx.AsyncClient(transport=env["transport"], base_url="http://t") as c:
        r = await c.post(f"/admin/orders/{oid}/approve", follow_redirects=False)
        assert r.status_code == 302  # login redirect


async def test_approve_requires_permission(env) -> None:
    oid, _uid = await env["seed"]()
    support = await env["login"]("support")  # no process_payments
    r = await support.post(f"/admin/orders/{oid}/approve", follow_redirects=False)
    assert r.status_code == 403


async def test_admin_approve_delivers(env) -> None:
    oid, _uid = await env["seed"]()
    admin = await env["login"]("admin")
    r = await admin.post(f"/admin/orders/{oid}/approve", follow_redirects=False)
    assert r.status_code == 303 and "saved=approved" in r.headers["location"]
    async with env["maker"]() as s:
        o = await order_service.get_order(s, oid)
    assert o.status == "delivered" and o.delivered_payload == "LIC-1"


async def test_add_balance_requires_permission(env) -> None:
    oid, _uid = await env["seed"]()
    viewer = await env["login"]("viewer")
    r = await viewer.post(f"/admin/orders/{oid}/add-balance",
                          data={"amount": "1000", "reason": "x"}, follow_redirects=False)
    assert r.status_code == 403


async def test_add_balance_creates_transaction_and_audit(env) -> None:
    oid, uid = await env["seed"]()
    admin = await env["login"]("admin")
    r = await admin.post(f"/admin/orders/{oid}/add-balance",
                         data={"amount": "12000", "reason": "goodwill"}, follow_redirects=False)
    assert "saved=wallet_added" in r.headers["location"]
    async with env["maker"]() as s:
        u = await user_service.get_by_id(s, uid)
        txns = (await s.execute(select(WalletTransaction))).scalars().all()
        actions = [a.action for a in (await s.execute(select(AuditLog))).scalars().all()]
    assert u.wallet_balance == 12000
    assert any(t.amount == 12000 for t in txns)
    assert "admin_wallet_added_from_receipt_review" in actions


async def test_subtract_balance_prevents_negative(env) -> None:
    oid, uid = await env["seed"]()
    admin = await env["login"]("admin")
    r = await admin.post(f"/admin/orders/{oid}/subtract-balance",
                         data={"amount": "5000", "reason": "x"}, follow_redirects=False)
    assert "error=" in r.headers["location"]
    async with env["maker"]() as s:
        u = await user_service.get_by_id(s, uid)
    assert u.wallet_balance == 0  # unchanged


async def test_block_user_from_order(env) -> None:
    oid, uid = await env["seed"]()
    admin = await env["login"]("admin")
    r = await admin.post(f"/admin/orders/{oid}/block-user",
                         data={"reason": "fraud"}, follow_redirects=False)
    assert "saved=user_blocked" in r.headers["location"]
    async with env["maker"]() as s:
        u = await user_service.get_by_id(s, uid)
        actions = [a.action for a in (await s.execute(select(AuditLog))).scalars().all()]
    assert u.is_blocked
    assert "user_blocked_from_receipt_review" in actions


async def test_restrict_user_from_order(env) -> None:
    oid, uid = await env["seed"]()
    admin = await env["login"]("admin")
    r = await admin.post(f"/admin/orders/{oid}/restrict-user",
                         data={"reason": "watch"}, follow_redirects=False)
    assert "saved=user_restricted" in r.headers["location"]
    async with env["maker"]() as s:
        u = await user_service.get_by_id(s, uid)
    assert u.is_restricted and u.restriction_reason == "watch"


async def test_accountant_cannot_block_but_can_adjust(env) -> None:
    oid, _uid = await env["seed"]()
    acc = await env["login"]("accountant")
    r = await acc.post(f"/admin/orders/{oid}/block-user", data={"reason": "x"},
                       follow_redirects=False)
    assert r.status_code == 403
    r = await acc.post(f"/admin/orders/{oid}/add-balance",
                       data={"amount": "500", "reason": "ok"}, follow_redirects=False)
    assert "saved=wallet_added" in r.headers["location"]


async def test_support_can_block_not_approve(env) -> None:
    oid, uid = await env["seed"]()
    support = await env["login"]("support")
    r = await support.post(f"/admin/orders/{oid}/approve", follow_redirects=False)
    assert r.status_code == 403
    r = await support.post(f"/admin/orders/{oid}/block-user", data={"reason": "x"},
                           follow_redirects=False)
    assert "saved=user_blocked" in r.headers["location"]

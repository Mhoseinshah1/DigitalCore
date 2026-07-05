"""Web orders + receipt serving: auth, pending queue, safe file access."""
from __future__ import annotations

from collections.abc import AsyncIterator

import httpx
import pytest_asyncio
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker, create_async_engine
from sqlalchemy.pool import StaticPool

from app.core.security import hash_password
from app.database import get_session
from app.models import Admin, Base, Payment, Product, User
from app.services import order_service, payment_service
from app.services.payment_service import ReceiptFile
from app.web.main import app

PASSWORD = "orders-web-1"


@pytest_asyncio.fixture
async def env(monkeypatch, tmp_path) -> AsyncIterator[dict]:
    engine = create_async_engine(
        "sqlite+aiosqlite://", poolclass=StaticPool,
        connect_args={"check_same_thread": False},
    )
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
        username = f"ordweb_{role}"
        async with maker() as s:
            s.add(Admin(username=username, password_hash=hash_password(PASSWORD),
                        is_active=True, is_super_admin=(role == "owner"), role=role))
            await s.commit()
        client = httpx.AsyncClient(transport=transport, base_url="http://t")
        clients.append(client)
        r = await client.post("/admin/login",
                              data={"username": username, "password": PASSWORD},
                              follow_redirects=False)
        assert r.status_code == 302
        return client

    try:
        yield {"maker": maker, "login": login, "transport": transport, "tmp": tmp_path}
    finally:
        for c in clients:
            await c.aclose()
        app.dependency_overrides.pop(get_session, None)
        await engine.dispose()


async def _seed_order_with_receipt(maker) -> tuple[int, int]:
    """Return (order_id, payment_id) for a waiting_admin order with a receipt."""
    async with maker() as s:
        u = User(telegram_id=777, username="buyer", first_name="B")
        s.add(u)
        p = Product(type="license", title="Gold Key", price=120000,
                    is_active=True, is_hidden=False)
        s.add(p)
        await s.flush()
        order = await order_service.create_order(s, u.id, p.id)
        await payment_service.create_payment_for_order(s, order)
        fi = ReceiptFile(content=b"\x89PNG\r\n\x1a\n" + b"x" * 40,
                         original_name="r.png", mime_type="image/png", file_id="f1")
        payment = await payment_service.submit_receipt(s, order.id, u.id, fi)
        await s.commit()
        return order.id, payment.id


async def test_orders_requires_auth(env) -> None:
    async with httpx.AsyncClient(transport=env["transport"], base_url="http://t") as c:
        r = await c.get("/admin/orders", follow_redirects=False)
        assert r.status_code == 302  # redirected to login


async def test_orders_opens_for_admin(env) -> None:
    await _seed_order_with_receipt(env["maker"])
    client = await env["login"]("admin")
    r = await client.get("/admin/orders")
    assert r.status_code == 200
    assert "DC-" in r.text and "buyer" in r.text


async def test_viewer_forbidden_from_orders(env) -> None:
    client = await env["login"]("viewer")  # no view_payments
    r = await client.get("/admin/orders")
    assert r.status_code == 403


async def test_pending_receipts_shows_only_waiting_admin(env) -> None:
    order_id, _pid = await _seed_order_with_receipt(env["maker"])
    # A second order left in pending_payment must NOT appear on the pending page.
    async with env["maker"]() as s:
        u = User(telegram_id=888, username="other", first_name="O")
        s.add(u)
        p = Product(type="license", title="Silver", price=1000, is_active=True, is_hidden=False)
        s.add(p)
        await s.flush()
        pending = await order_service.create_order(s, u.id, p.id)
        await s.commit()
        pending_number = pending.order_number

    client = await env["login"]("admin")
    r = await client.get("/admin/orders/pending-receipts")
    assert r.status_code == 200
    assert "buyer" in r.text
    assert pending_number not in r.text  # pending_payment order excluded


async def test_order_detail_opens(env) -> None:
    order_id, pid = await _seed_order_with_receipt(env["maker"])
    client = await env["login"]("admin")
    r = await client.get(f"/admin/orders/{order_id}")
    assert r.status_code == 200
    assert f"/admin/receipts/{pid}" in r.text


async def test_receipt_requires_auth(env) -> None:
    _oid, pid = await _seed_order_with_receipt(env["maker"])
    async with httpx.AsyncClient(transport=env["transport"], base_url="http://t") as c:
        r = await c.get(f"/admin/receipts/{pid}", follow_redirects=False)
        assert r.status_code == 302


async def test_receipt_served_to_admin(env) -> None:
    _oid, pid = await _seed_order_with_receipt(env["maker"])
    client = await env["login"]("admin")
    r = await client.get(f"/admin/receipts/{pid}")
    assert r.status_code == 200
    assert r.headers["content-type"].startswith("image/")
    assert "inline" in r.headers.get("content-disposition", "")


async def test_receipt_path_traversal_blocked(env) -> None:
    # Craft a payment whose stored path tries to escape the receipts root.
    async with env["maker"]() as s:
        u = User(telegram_id=999, first_name="X")
        s.add(u)
        p = Product(type="license", title="Z", price=1000, is_active=True, is_hidden=False)
        s.add(p)
        await s.flush()
        order = await order_service.create_order(s, u.id, p.id)
        pay = Payment(order_id=order.id, user_id=u.id, amount=1000,
                      method="card_to_card", status="receipt_submitted",
                      receipt_path="../../../../etc/passwd", receipt_mime_type="text/plain")
        s.add(pay)
        await s.commit()
        pid = pay.id

    client = await env["login"]("admin")
    r = await client.get(f"/admin/receipts/{pid}")
    assert r.status_code == 404  # traversal refused, treated as missing


async def test_receipt_missing_is_404(env) -> None:
    client = await env["login"]("admin")
    r = await client.get("/admin/receipts/999999")
    assert r.status_code == 404

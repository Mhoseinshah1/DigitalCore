"""Admin web: Payment Core pages (payments list/pending/detail, approve/reject
RBAC, payment-method CRUD, secrets never rendered)."""
from __future__ import annotations

from collections.abc import AsyncIterator

import httpx
import pytest_asyncio
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker, create_async_engine
from sqlalchemy.pool import StaticPool

from app.core.security import hash_password
from app.database import get_session
from app.models import Admin, Base, User
from app.models.invoice import Invoice
from app.models.payment import Payment
from app.models.payment_method import PaymentMethod
from app.web.main import app

PW = "pay-web-1"


@pytest_asyncio.fixture
async def env() -> AsyncIterator[dict]:
    engine = create_async_engine("sqlite+aiosqlite://", poolclass=StaticPool,
                                 connect_args={"check_same_thread": False})
    async with engine.begin() as conn:
        await conn.run_sync(Base.metadata.create_all)
    maker = async_sessionmaker(engine, class_=AsyncSession, expire_on_commit=False)

    async def _override():
        async with maker() as s:
            yield s

    app.dependency_overrides[get_session] = _override

    async with maker() as s:
        for role in ("owner", "viewer"):
            s.add(Admin(username=f"pw_{role}", password_hash=hash_password(PW),
                        is_active=True, is_super_admin=(role == "owner"), role=role))
        user = User(telegram_id=555, first_name="Buyer", username="buyer",
                    wallet_balance=0)
        s.add(user)
        await s.flush()
        inv = Invoice(invoice_number="INV-20260101-TEST01", tracking_code="INV-TESTTRACK1",
                      user_id=user.id, invoice_type="wallet_topup",
                      amount=80_000, final_amount=80_000, status="unpaid")
        s.add(inv)
        await s.flush()
        s.add(Payment(user_id=user.id, invoice_id=inv.id, amount=80_000,
                      payment_type="wallet_topup", method="card_to_card",
                      status="receipt_submitted", tracking_code="PAY-TESTTRACK1"))
        s.add(PaymentMethod(code="manual_receipt", title="کارت به کارت",
                            method_type="manual_receipt", is_active=True))
        await s.commit()
        payment_id = (await s.execute(
            Payment.__table__.select())).mappings().first()["id"]

    async def login(role: str) -> httpx.AsyncClient:
        c = httpx.AsyncClient(transport=httpx.ASGITransport(app=app),
                              base_url="http://t")
        r = await c.post("/admin/login",
                         data={"username": f"pw_{role}", "password": PW},
                         follow_redirects=False)
        assert r.status_code == 302
        return c

    try:
        yield {"login": login, "maker": maker, "payment_id": payment_id}
    finally:
        app.dependency_overrides.pop(get_session, None)
        await engine.dispose()


async def test_payments_pages_render(env) -> None:
    owner = await env["login"]("owner")
    r = await owner.get("/admin/payments")
    assert r.status_code == 200 and "PAY-TESTTRACK1" in r.text
    r = await owner.get("/admin/payments/pending")
    assert r.status_code == 200
    assert "رسیدهای تایید نشده" in r.text  # required page title
    # RTL action order on pending rows: حذف → جزئیات → رد → تایید
    row = r.text
    i_del = row.find("حذف رسید"); i_det = row.find("مشاهده جزئیات")
    i_rej = row.find("رد پرداخت"); i_app = row.find("تایید پرداخت")
    assert -1 not in (i_del, i_det, i_rej, i_app)
    assert i_del < i_det < i_rej < i_app
    await owner.aclose()


async def test_payment_detail_and_approve_credits_wallet(env) -> None:
    owner = await env["login"]("owner")
    pid = env["payment_id"]
    r = await owner.get(f"/admin/payments/{pid}")
    assert r.status_code == 200 and "PAY-TESTTRACK1" in r.text

    r = await owner.post(f"/admin/payments/{pid}/approve", follow_redirects=False)
    assert r.status_code == 303 and "saved=1" in r.headers["location"]
    async with env["maker"]() as s:
        payment = await s.get(Payment, pid)
        assert payment.status == "approved"
        user = await s.get(User, payment.user_id)
        assert int(user.wallet_balance) == 80_000  # topup credited
    await owner.aclose()


async def test_viewer_cannot_approve_or_reject(env) -> None:
    viewer = await env["login"]("viewer")
    pid = env["payment_id"]
    r = await viewer.post(f"/admin/payments/{pid}/approve", follow_redirects=False)
    assert r.status_code in (302, 303, 403)
    if r.status_code in (302, 303):  # redirected away, not processed
        assert "saved" not in r.headers.get("location", "")
    async with env["maker"]() as s:
        payment = await s.get(Payment, pid)
        assert payment.status == "receipt_submitted"
    await viewer.aclose()


async def test_reject_requires_reason_and_stores_it(env) -> None:
    owner = await env["login"]("owner")
    pid = env["payment_id"]
    r = await owner.post(f"/admin/payments/{pid}/reject", data={"reason": ""},
                         follow_redirects=False)
    assert "error=" in r.headers["location"]
    r = await owner.post(f"/admin/payments/{pid}/reject",
                         data={"reason": "مبلغ ناقص"}, follow_redirects=False)
    assert "saved=1" in r.headers["location"]
    async with env["maker"]() as s:
        payment = await s.get(Payment, pid)
        assert payment.status == "rejected" and payment.reject_reason == "مبلغ ناقص"
    await owner.aclose()


async def test_payment_methods_crud_and_secrets_hidden(env) -> None:
    owner = await env["login"]("owner")
    r = await owner.get("/admin/payment-methods")
    assert r.status_code == 200 and "manual_receipt" in r.text

    r = await owner.post("/admin/payment-methods/create", data={
        "code": "zarinpal", "title": "زرین‌پال", "method_type": "online_gateway",
        "sort_order": "5", "cashback_percent": "2", "activate_after_payments": "0",
        "api_token": "SUPER-SECRET-TOKEN", "merchant_id": "MERCHANT-42",
        "is_active": "on",
    }, follow_redirects=False)
    assert "saved=1" in r.headers["location"]

    async with env["maker"]() as s:
        method = (await s.execute(
            PaymentMethod.__table__.select().where(
                PaymentMethod.__table__.c.code == "zarinpal"))).mappings().first()
        assert method is not None
        # Stored encrypted, not plaintext.
        assert method["api_token_encrypted"] and "SUPER-SECRET-TOKEN" not in method["api_token_encrypted"]
        method_id = method["id"]

    # Secrets never appear on the edit page or listing.
    r = await owner.get(f"/admin/payment-methods/{method_id}/edit")
    assert r.status_code == 200
    assert "SUPER-SECRET-TOKEN" not in r.text and "MERCHANT-42" not in r.text
    assert "تنظیم‌شده" in r.text  # configured badge instead

    r = await owner.post(f"/admin/payment-methods/{method_id}/toggle-active",
                         follow_redirects=False)
    assert "saved=1" in r.headers["location"]
    await owner.aclose()


async def test_viewer_cannot_manage_methods(env) -> None:
    viewer = await env["login"]("viewer")
    r = await viewer.get("/admin/payment-methods", follow_redirects=False)
    assert r.status_code in (302, 303, 403)
    await viewer.aclose()

"""Phase 11 export_service: CSV headers, UTF-8 BOM, and secret-free content."""
from __future__ import annotations

from datetime import datetime, timezone

import pytest_asyncio
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker, create_async_engine
from sqlalchemy.pool import StaticPool

from app.models import (
    Base, Coupon, CouponUsage, LicenseItem, Order, Payment, Product,
    ReferralReward, Ticket, User, V2RayService, WalletTransaction,
)
from app.services import export_service as E

NOW = datetime.now(timezone.utc)
BOM = "﻿"


@pytest_asyncio.fixture
async def db():
    engine = create_async_engine("sqlite+aiosqlite://", poolclass=StaticPool,
                                 connect_args={"check_same_thread": False})
    async with engine.begin() as conn:
        await conn.run_sync(Base.metadata.create_all)
    maker = async_sessionmaker(engine, class_=AsyncSession, expire_on_commit=False)
    try:
        yield maker
    finally:
        await engine.dispose()


async def _seed(maker):
    async with maker() as s:
        u = User(telegram_id=1, first_name="A", wallet_balance=5000,
                 phone_number="0912SECRET", referral_code="REF1")
        s.add(u)
        await s.flush()
        p = Product(type="license", title="Gold", price=100_000, is_active=True)
        s.add(p)
        await s.flush()
        o = Order(order_number="DC-1", user_id=u.id, product_id=p.id, amount=100_000,
                  final_amount=90_000, status="delivered", delivered_at=NOW)
        s.add(o)
        await s.flush()
        s.add(Payment(order_id=o.id, user_id=u.id, amount=90_000, status="approved"))
        s.add(WalletTransaction(user_id=u.id, amount=5000, balance_after=5000,
                                type="deposit", status="completed"))
        s.add(LicenseItem(product_id=p.id, email="a@b.com", password="SUPERSECRETPW",
                          status="sold", sold_to_user_id=u.id, order_id=o.id, sold_at=NOW))
        s.add(V2RayService(user_id=u.id, order_id=o.id, product_id=p.id, xui_server_id=1,
                           xui_inbound_id=1, client_email="dc@x",
                           client_uuid="abcdef-UUIDSECRET-9999",
                           subscription_url="https://panel/sub/SECRETSUB",
                           total_gb=1000, used_gb=400, status="active"))
        cp = Coupon(code="SAVE10", discount_type="percent", discount_value=10)
        s.add(cp)
        await s.flush()
        s.add(CouponUsage(coupon_id=cp.id, user_id=u.id, order_id=o.id, discount_amount=10_000))
        s.add(ReferralReward(referrer_user_id=u.id, referred_user_id=u.id, order_id=o.id,
                             reward_type="fixed", reward_amount=5000, status="pending"))
        s.add(Ticket(ticket_number="TK-1", user_id=u.id, subject="Hi", status="open",
                     priority="high"))
        await s.commit()


def _header(csv: str) -> str:
    assert csv.startswith(BOM), "missing UTF-8 BOM"
    return csv[len(BOM):].splitlines()[0]


async def test_orders_csv_has_headers(db) -> None:
    await _seed(db)
    async with db() as s:
        csv = await E.export_orders_csv(s)
    assert "order_number" in _header(csv) and "final_amount" in _header(csv)


async def test_payments_csv_has_headers(db) -> None:
    await _seed(db)
    async with db() as s:
        csv = await E.export_payments_csv(s)
    assert "amount" in _header(csv) and "method" in _header(csv)


async def test_wallet_csv_has_headers(db) -> None:
    await _seed(db)
    async with db() as s:
        csv = await E.export_wallet_transactions_csv(s)
    assert "balance_after" in _header(csv) and "type" in _header(csv)


async def test_users_csv_has_headers_no_phone(db) -> None:
    await _seed(db)
    async with db() as s:
        csv = await E.export_users_csv(s)
    header = _header(csv)
    assert "telegram_id" in header  # already exposed in the admin user list
    assert "phone" not in header.lower()
    assert "0912SECRET" not in csv       # phone value never leaks


async def test_licenses_csv_never_includes_password(db) -> None:
    await _seed(db)
    async with db() as s:
        csv = await E.export_licenses_csv(s)
    header = _header(csv)
    assert "password" not in header.lower()
    assert "SUPERSECRETPW" not in csv
    # documented columns are present
    for col in ("email", "product", "status", "sold_to_user_id", "order_id", "sold_at"):
        assert col in header


async def test_v2ray_csv_masks_uuid_and_drops_credentials(db) -> None:
    await _seed(db)
    async with db() as s:
        csv = await E.export_v2ray_services_csv(s)
    assert "UUIDSECRET" not in csv          # raw uuid never exported
    assert "****9999" in csv                # masked instead
    assert "SECRETSUB" not in csv           # subscription url dropped
    assert "subscription_url" not in _header(csv).lower()


async def test_optional_exports_have_headers(db) -> None:
    await _seed(db)
    async with db() as s:
        for fn in (E.export_coupon_usages_csv, E.export_referral_rewards_csv,
                   E.export_tickets_csv):
            csv = await fn(s)
            assert csv.startswith(BOM) and len(_header(csv)) > 0


async def test_products_csv_is_utf8_and_bom(db) -> None:
    await _seed(db)
    async with db() as s:
        csv = await E.export_products_csv(s)
    csv.encode("utf-8")  # must be encodable as UTF-8
    assert csv.startswith(BOM)
    assert E.export_filename("orders").endswith(".csv")

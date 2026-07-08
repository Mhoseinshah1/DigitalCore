"""Phase 11 report_service: date helpers + SQL aggregation correctness."""
from __future__ import annotations

from datetime import datetime, timedelta, timezone

import pytest_asyncio
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker, create_async_engine
from sqlalchemy.pool import StaticPool

from app.models import (
    Base, Coupon, CouponUsage, LicenseItem, Order, Payment, Product,
    ReferralReward, Ticket, User, V2RayService, WalletTransaction,
    WalletTopupRequest,
)
from app.services import report_service as R

UTC = timezone.utc
NOW = datetime.now(UTC)


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
        u1 = User(telegram_id=1, first_name="A", wallet_balance=5000, last_activity_at=NOW)
        u2 = User(telegram_id=2, first_name="B", is_blocked=True)
        s.add_all([u1, u2])
        await s.flush()
        p = Product(type="license", title="Gold", price=100_000, is_active=True)
        pv = Product(type="v2ray", title="VPN", price=200_000, is_active=True)
        s.add_all([p, pv])
        await s.flush()
        o1 = Order(order_number="DC-1", user_id=u1.id, product_id=p.id, amount=100_000,
                   final_amount=90_000, discount_amount=10_000, status="delivered",
                   payment_method="card_to_card", delivered_at=NOW)
        o2 = Order(order_number="DC-2", user_id=u1.id, product_id=pv.id, amount=200_000,
                   final_amount=200_000, status="failed", payment_method="wallet",
                   delivery_error="boom")
        s.add_all([o1, o2])
        await s.flush()
        s.add(Payment(order_id=o1.id, user_id=u1.id, amount=90_000, method="card_to_card",
                      status="approved"))
        s.add(Payment(order_id=o2.id, user_id=u1.id, amount=200_000, method="wallet",
                      status="receipt_submitted"))
        s.add(WalletTransaction(user_id=u1.id, amount=5000, balance_after=5000,
                                type="deposit", status="completed"))
        s.add(WalletTransaction(user_id=u1.id, amount=-2000, balance_after=3000,
                                type="purchase", status="completed"))
        s.add(WalletTopupRequest(user_id=u1.id, amount=5000, status="approved"))
        s.add(LicenseItem(product_id=p.id, email="a@b.com", password="pw1", status="available"))
        s.add(LicenseItem(product_id=p.id, email="c@d.com", password="pw2", status="sold",
                          sold_to_user_id=u1.id, order_id=o1.id, sold_at=NOW))
        s.add(V2RayService(user_id=u1.id, order_id=o1.id, product_id=pv.id, xui_server_id=1,
                           xui_inbound_id=1, client_email="dc-u1-o1@x", client_uuid="uuid-1234",
                           total_gb=1000, used_gb=400, status="active",
                           expire_at=NOW + timedelta(days=2)))
        cp = Coupon(code="SAVE10", discount_type="percent", discount_value=10, is_active=True)
        s.add(cp)
        await s.flush()
        s.add(CouponUsage(coupon_id=cp.id, user_id=u1.id, order_id=o1.id, discount_amount=10_000))
        s.add(ReferralReward(referrer_user_id=u1.id, referred_user_id=u2.id, order_id=o1.id,
                             reward_type="fixed", reward_amount=5000, status="pending"))
        s.add(Ticket(ticket_number="TK-1", user_id=u1.id, subject="Help", status="open",
                     priority="high"))
        await s.commit()


# --- date helpers ---------------------------------------------------------
def test_date_range_presets() -> None:
    s, e = R.parse_date_range(preset="last_7_days")
    assert (e - s).days == 7
    s, e = R.parse_date_range(preset="this_month")
    assert s.day == 1 and e.day == 1
    s, e = R.parse_date_range(preset="last_month")
    assert s.day == 1 and e.day == 1
    assert R.parse_date_range(preset="bogus") == (None, None)


def test_date_range_custom_inclusive() -> None:
    s, e = R.parse_date_range("2026-01-01", "2026-01-31")
    assert s.isoformat().startswith("2026-01-01")
    # end is the exclusive midnight of the *next* day so the 31st is included
    assert e.isoformat().startswith("2026-02-01")


def test_previous_period() -> None:
    s, e = R.parse_date_range("2026-02-01", "2026-02-28")
    ps, pe = R.get_previous_period(s, e)
    assert pe == s and (e - s) == (s - ps)
    assert R.get_previous_period(None, None) == (None, None)


def test_safe_percent_change_handles_zero() -> None:
    assert R.safe_percent_change(0, 0) == 0.0
    assert R.safe_percent_change(5, 0) is None          # no baseline
    assert R.safe_percent_change(150, 100) == 50.0
    assert R.safe_percent_change(50, 100) == -50.0


# --- aggregations ---------------------------------------------------------
async def test_dashboard_summary_keys(db) -> None:
    await _seed(db)
    async with db() as s:
        out = await R.get_dashboard_summary(s)
    for key in ("revenue", "orders", "users", "products", "attention",
                "top_products", "v2ray", "capabilities"):
        assert key in out, key
    assert out["capabilities"]["coupons"] is True
    assert out["attention"]["pending_receipts"] == 1
    assert out["attention"]["failed_orders"] == 1


async def test_revenue_and_sales_by_day(db) -> None:
    await _seed(db)
    async with db() as s:
        rev = await R.get_revenue_summary(s)
        by_day = await R.get_sales_by_day(s)
    assert rev["total"] == 90_000 and rev["orders"] == 1
    assert any(d["revenue"] == 90_000 for d in by_day)


async def test_orders_by_status(db) -> None:
    await _seed(db)
    async with db() as s:
        rows = await R.get_orders_by_status(s)
    m = {r["status"]: r["count"] for r in rows}
    assert m["delivered"] == 1 and m["failed"] == 1


async def test_payments_by_method(db) -> None:
    await _seed(db)
    async with db() as s:
        rows = await R.get_payments_by_method(s)  # approved only
    # only the approved card_to_card payment counts
    assert {r["method"] for r in rows} == {"card_to_card"}
    assert rows[0]["amount"] == 90_000


async def test_wallet_transaction_summary(db) -> None:
    await _seed(db)
    async with db() as s:
        rows = await R.get_wallet_transaction_summary(s)
        changes = await R.get_wallet_balance_changes(s)
    types = {r["type"] for r in rows}
    assert {"deposit", "purchase"} <= types
    assert changes["credits"] == 5000 and changes["debits"] == -2000 and changes["net"] == 3000


async def test_top_products_by_revenue(db) -> None:
    await _seed(db)
    async with db() as s:
        rows = await R.get_top_products_by_revenue(s)
    assert rows and rows[0]["title"] == "Gold" and rows[0]["revenue"] == 90_000


async def test_user_growth_by_day(db) -> None:
    await _seed(db)
    async with db() as s:
        rows = await R.get_user_growth_by_day(s)
        new = await R.get_new_users(s)
        active = await R.get_active_users(s)
    assert new == 2 and active == 1
    assert rows[-1]["cumulative"] >= 2


async def test_license_and_v2ray_summaries(db) -> None:
    await _seed(db)
    async with db() as s:
        stock = await R.get_license_stock_summary(s)
        low = await R.get_low_stock_license_products(s, threshold=5)
        v2 = await R.get_v2ray_service_summary(s)
        expiring = await R.get_v2ray_expiring_soon(s, days=7)
    assert stock["available"] == 1 and stock["sold"] == 1
    assert low and low[0]["available"] == 1
    assert v2["active"] == 1
    assert len(expiring) == 1


async def test_optional_models_summaries(db) -> None:
    await _seed(db)
    async with db() as s:
        coupons = await R.get_coupon_usage_summary(s)
        referrals = await R.get_referral_reward_summary(s)
        tickets = await R.get_ticket_summary(s)
        open_t = await R.get_open_ticket_summary(s)
    assert coupons["available"] and coupons["uses"] == 1 and coupons["total_discount"] == 10_000
    assert referrals["available"] and referrals["pending_count"] == 1
    assert tickets["available"] and tickets["total"] == 1
    assert open_t["open"] == 1


async def test_empty_db_does_not_crash(db) -> None:
    # No rows seeded: every summary must return safe zero-ish structures.
    async with db() as s:
        assert (await R.get_revenue_summary(s))["total"] == 0
        assert await R.get_sales_by_day(s) == [] or isinstance(await R.get_sales_by_day(s), list)
        assert (await R.get_dashboard_summary(s))["orders"]["total"] == 0
        assert (await R.get_license_stock_summary(s))["total"] == 0

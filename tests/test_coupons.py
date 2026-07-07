"""Phase 10: coupon_service — normalization, discount math, validation rules,
apply/remove on an order, and race-safe idempotent consumption."""
from __future__ import annotations

from datetime import datetime, timedelta, timezone

import pytest

from app.models import Product, User
from app.services import coupon_service as C
from app.services import order_service
from app.services.coupon_service import CouponError


def _now():
    return datetime.now(timezone.utc)


async def _user(db_session, tg=1) -> User:
    u = User(telegram_id=tg, first_name="U")
    db_session.add(u)
    await db_session.flush()
    return u


async def _product(db_session, *, type_="license", price=100_000) -> Product:
    p = Product(type=type_, title="P", price=price, is_active=True,
                duration_days=(30 if type_ == "v2ray" else None),
                traffic_gb=(50 if type_ == "v2ray" else None))
    db_session.add(p)
    await db_session.flush()
    return p


def test_normalize_code() -> None:
    assert C.normalize_code("  save20 ") == "SAVE20"
    assert C.normalize_code(None) == ""


async def test_percent_and_fixed_and_cap(db_session) -> None:
    pct = await C.create_coupon(db_session, code="P20", discount_type="percent",
                                discount_value=20, max_discount_amount=15_000)
    fix = await C.create_coupon(db_session, code="F5", discount_type="fixed", discount_value=5_000)
    await db_session.commit()
    assert C.calculate_discount(pct, 100_000) == 15_000   # 20% capped at 15k
    assert C.calculate_discount(pct, 50_000) == 10_000     # 20% under the cap
    assert C.calculate_discount(fix, 100_000) == 5_000
    assert C.calculate_discount(fix, 3_000) == 3_000       # never exceeds the amount


async def test_percent_bounds_and_fixed_positive(db_session) -> None:
    with pytest.raises(CouponError):
        await C.create_coupon(db_session, code="BAD", discount_type="percent", discount_value=0)
    with pytest.raises(CouponError):
        await C.create_coupon(db_session, code="BAD", discount_type="percent", discount_value=101)
    with pytest.raises(CouponError):
        await C.create_coupon(db_session, code="BAD", discount_type="fixed", discount_value=0)


async def test_expired_and_inactive_rejected(db_session) -> None:
    u = await _user(db_session)
    p = await _product(db_session)
    exp = await C.create_coupon(db_session, code="OLD", discount_type="fixed", discount_value=1_000,
                                expires_at=_now() - timedelta(days=1))
    off = await C.create_coupon(db_session, code="OFF", discount_type="fixed", discount_value=1_000,
                                is_active=False)
    nyet = await C.create_coupon(db_session, code="SOON", discount_type="fixed", discount_value=1_000,
                                 starts_at=_now() + timedelta(days=1))
    await db_session.commit()
    for code, err in (("OLD", "coupon_expired"), ("OFF", "coupon_inactive"),
                      ("SOON", "coupon_not_started")):
        with pytest.raises(CouponError) as exc:
            await C.validate_coupon(db_session, code, u.id, p.id, 100_000)
        assert exc.value.code == err


async def test_min_order_amount(db_session) -> None:
    u = await _user(db_session)
    p = await _product(db_session)
    await C.create_coupon(db_session, code="MIN", discount_type="fixed", discount_value=1_000,
                          min_order_amount=50_000)
    await db_session.commit()
    with pytest.raises(CouponError) as exc:
        await C.validate_coupon(db_session, "MIN", u.id, p.id, 40_000)
    assert exc.value.code == "min_order_amount_not_met"
    coupon, disc = await C.validate_coupon(db_session, "MIN", u.id, p.id, 60_000)
    assert disc == 1_000


async def test_product_and_type_and_action_restrictions(db_session) -> None:
    u = await _user(db_session)
    lic = await _product(db_session, type_="license")
    v2 = await _product(db_session, type_="v2ray")
    # product-specific
    await C.create_coupon(db_session, code="ONLYLIC", discount_type="fixed", discount_value=1_000,
                          product_id=lic.id)
    # product-type
    await C.create_coupon(db_session, code="V2ONLY", discount_type="fixed", discount_value=1_000,
                          product_type="v2ray")
    # action
    await C.create_coupon(db_session, code="RENEWONLY", discount_type="fixed", discount_value=1_000,
                          applies_to_action="renew_service")
    await db_session.commit()
    with pytest.raises(CouponError) as e1:
        await C.validate_coupon(db_session, "ONLYLIC", u.id, v2.id, 100_000)
    assert e1.value.code == "product_not_allowed"
    with pytest.raises(CouponError) as e2:
        await C.validate_coupon(db_session, "V2ONLY", u.id, lic.id, 100_000)
    assert e2.value.code == "product_type_not_allowed"
    with pytest.raises(CouponError) as e3:
        await C.validate_coupon(db_session, "RENEWONLY", u.id, lic.id, 100_000,
                                action_type="new_purchase")
    assert e3.value.code == "action_not_allowed"
    # correct action passes
    coupon, _d = await C.validate_coupon(db_session, "RENEWONLY", u.id, v2.id, 100_000,
                                         action_type="renew_service")
    assert coupon.code == "RENEWONLY"


async def test_usage_and_per_user_limits(db_session) -> None:
    u = await _user(db_session)
    p = await _product(db_session)
    c = await C.create_coupon(db_session, code="LIM", discount_type="fixed", discount_value=1_000,
                              usage_limit=1, usage_limit_per_user=1)
    await db_session.commit()
    # Consume it via an order.
    o = await order_service.create_order(db_session, u.id, p.id)
    await C.apply_coupon_to_order(db_session, o.id, "LIM", u.id)
    o.status = "approved"
    await db_session.commit()
    assert await C.record_usage(db_session, o.id) is True
    await db_session.commit()
    # Global limit reached.
    with pytest.raises(CouponError) as exc:
        await C.validate_coupon(db_session, "LIM", u.id, p.id, 100_000)
    assert exc.value.code == "usage_limit_reached"


async def test_apply_remove_and_idempotent_consume(db_session) -> None:
    u = await _user(db_session)
    p = await _product(db_session, price=100_000)
    c = await C.create_coupon(db_session, code="TEN", discount_type="percent", discount_value=10)
    await db_session.commit()
    o = await order_service.create_order(db_session, u.id, p.id)
    await db_session.commit()
    o = await C.apply_coupon_to_order(db_session, o.id, "TEN", u.id)
    await db_session.commit()
    assert o.final_amount == 90_000 and o.discount_amount == 10_000 and o.coupon_code == "TEN"
    # Remove restores full price.
    o = await C.remove_coupon_from_order(db_session, o.id, u.id)
    await db_session.commit()
    assert o.final_amount == 100_000 and o.coupon_id is None
    # Re-apply + consume; a second consume is a no-op.
    await C.apply_coupon_to_order(db_session, o.id, "TEN", u.id)
    o.status = "approved"
    await db_session.commit()
    assert await C.record_usage(db_session, o.id) is True
    await db_session.commit()
    assert await C.record_usage(db_session, o.id) is False


async def test_cannot_change_coupon_after_payment(db_session) -> None:
    u = await _user(db_session)
    p = await _product(db_session)
    await C.create_coupon(db_session, code="TEN", discount_type="percent", discount_value=10)
    await db_session.commit()
    o = await order_service.create_order(db_session, u.id, p.id)
    o.status = "waiting_admin"  # receipt submitted → locked
    await db_session.commit()
    with pytest.raises(CouponError) as exc:
        await C.apply_coupon_to_order(db_session, o.id, "TEN", u.id)
    assert exc.value.code == "order_locked"


async def test_apply_rejects_other_users_order(db_session) -> None:
    a = await _user(db_session, tg=1)
    b = await _user(db_session, tg=2)
    p = await _product(db_session)
    await C.create_coupon(db_session, code="TEN", discount_type="percent", discount_value=10)
    await db_session.commit()
    o = await order_service.create_order(db_session, a.id, p.id)
    await db_session.commit()
    with pytest.raises(CouponError) as exc:
        await C.apply_coupon_to_order(db_session, o.id, "TEN", b.id)
    assert exc.value.code == "not_your_order"


async def test_duplicate_code_rejected(db_session) -> None:
    await C.create_coupon(db_session, code="DUP", discount_type="fixed", discount_value=1_000)
    await db_session.commit()
    with pytest.raises(CouponError) as exc:
        await C.create_coupon(db_session, code=" dup ", discount_type="fixed", discount_value=1_000)
    assert exc.value.code == "code_exists"

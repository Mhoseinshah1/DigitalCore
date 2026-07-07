"""Phase 10: referral_service — codes, registration guards, and reward rules
(first-order-only, min amount, auto payout, approval flow, idempotency)."""
from __future__ import annotations

from datetime import datetime, timezone

import pytest

from app.core.settings_service import SettingsService
from app.models import Order, User
from app.services import referral_service as R


def _now():
    return datetime.now(timezone.utc)


async def _user(db_session, tg, *, balance=0) -> User:
    u = User(telegram_id=tg, first_name=f"U{tg}", wallet_balance=balance)
    db_session.add(u)
    await db_session.flush()
    return u


async def _delivered_order(db_session, user_id, *, final=100_000, number="DC-R-1") -> Order:
    o = Order(order_number=number, user_id=user_id, product_id=None, amount=final,
              final_amount=final, status="delivered", payment_method="wallet",
              paid_at=_now(), delivered_at=_now())
    # product_id is NOT NULL — attach a throwaway product.
    from app.models import Product
    p = Product(type="license", title="X", price=final, is_active=True)
    db_session.add(p)
    await db_session.flush()
    o.product_id = p.id
    db_session.add(o)
    await db_session.flush()
    return o


async def _set(db_session, **kv):
    ss = SettingsService(db_session)
    for k, v in kv.items():
        await ss.set(k, v)


# --- codes + registration ---------------------------------------------------
async def test_code_generation_and_reuse(db_session) -> None:
    u = await _user(db_session, 1)
    await db_session.commit()
    code = await R.get_or_create_referral_code(db_session, u.id)
    assert code and len(code) == 8
    again = await R.get_or_create_referral_code(db_session, u.id)
    assert again == code  # stable


async def test_register_referral_and_guards(db_session) -> None:
    ref = await _user(db_session, 1)
    new = await _user(db_session, 2)
    await db_session.commit()
    code = await R.get_or_create_referral_code(db_session, ref.id)
    await db_session.commit()
    # invalid code ignored
    assert await R.register_referral(db_session, new.id, "NOPE") is None
    # valid registers
    r = await R.register_referral(db_session, new.id, code)
    await db_session.commit()
    assert r.id == ref.id
    assert (await R.get_referrer(db_session, new.id)).id == ref.id
    # cannot overwrite
    other = await _user(db_session, 3)
    await db_session.commit()
    ocode = await R.get_or_create_referral_code(db_session, other.id)
    await db_session.commit()
    assert await R.register_referral(db_session, new.id, ocode) is None
    assert (await R.get_referrer(db_session, new.id)).id == ref.id


async def test_self_referral_rejected(db_session) -> None:
    u = await _user(db_session, 1)
    await db_session.commit()
    code = await R.get_or_create_referral_code(db_session, u.id)
    await db_session.commit()
    assert await R.register_referral(db_session, u.id, code) is None
    assert (await R.get_referrer(db_session, u.id)) is None


# --- rewards ----------------------------------------------------------------
async def test_auto_reward_paid_to_wallet(db_session) -> None:
    await _set(db_session, referral_reward_value="5000", referral_reward_type="fixed",
               referral_reward_requires_admin_approval="false")
    ref = await _user(db_session, 1, balance=0)
    buyer = await _user(db_session, 2)
    await db_session.commit()
    code = await R.get_or_create_referral_code(db_session, ref.id)
    await db_session.commit()
    await R.register_referral(db_session, buyer.id, code)
    await db_session.commit()
    o = await _delivered_order(db_session, buyer.id)
    await db_session.commit()
    reward = await R.create_reward_for_order(db_session, o.id)
    assert reward is not None and reward.status == "paid" and reward.reward_amount == 5000
    assert (await db_session.get(User, ref.id)).wallet_balance == 5000
    # idempotent
    assert await R.create_reward_for_order(db_session, o.id) is None
    assert len(await R.list_rewards(db_session)) == 1


async def test_approval_required_stays_pending_then_admin_pays(db_session) -> None:
    await _set(db_session, referral_reward_value="10", referral_reward_type="percent",
               referral_reward_requires_admin_approval="true")
    ref = await _user(db_session, 1, balance=0)
    buyer = await _user(db_session, 2)
    await db_session.commit()
    code = await R.get_or_create_referral_code(db_session, ref.id)
    await db_session.commit()
    await R.register_referral(db_session, buyer.id, code)
    await db_session.commit()
    o = await _delivered_order(db_session, buyer.id, final=100_000)
    await db_session.commit()
    reward = await R.create_reward_for_order(db_session, o.id)
    assert reward.status == "pending" and reward.reward_amount == 10_000  # 10%
    assert (await db_session.get(User, ref.id)).wallet_balance == 0
    # admin approves → paid + wallet credited
    paid = await R.approve_reward(db_session, reward.id, admin_id=7)
    assert paid.status == "paid"
    assert (await db_session.get(User, ref.id)).wallet_balance == 10_000
    # approving again is a no-op (no double credit)
    await R.approve_reward(db_session, reward.id, admin_id=7)
    assert (await db_session.get(User, ref.id)).wallet_balance == 10_000


async def test_reject_reward(db_session) -> None:
    await _set(db_session, referral_reward_value="5000",
               referral_reward_requires_admin_approval="true")
    ref = await _user(db_session, 1)
    buyer = await _user(db_session, 2)
    await db_session.commit()
    code = await R.get_or_create_referral_code(db_session, ref.id)
    await db_session.commit()
    await R.register_referral(db_session, buyer.id, code)
    await db_session.commit()
    o = await _delivered_order(db_session, buyer.id)
    await db_session.commit()
    reward = await R.create_reward_for_order(db_session, o.id)
    rejected = await R.reject_reward(db_session, reward.id, admin_id=7, reason="fraud")
    assert rejected.status == "rejected" and rejected.reject_reason == "fraud"
    assert (await db_session.get(User, ref.id)).wallet_balance == 0


async def test_first_order_only_respected(db_session) -> None:
    await _set(db_session, referral_reward_value="5000",
               referral_reward_requires_admin_approval="false",
               referral_reward_first_order_only="true")
    ref = await _user(db_session, 1)
    buyer = await _user(db_session, 2)
    await db_session.commit()
    code = await R.get_or_create_referral_code(db_session, ref.id)
    await db_session.commit()
    await R.register_referral(db_session, buyer.id, code)
    await db_session.commit()
    o1 = await _delivered_order(db_session, buyer.id, number="DC-R-1")
    o2 = await _delivered_order(db_session, buyer.id, number="DC-R-2")
    await db_session.commit()
    assert await R.create_reward_for_order(db_session, o1.id) is not None
    # second delivered order → no reward (first-order-only)
    assert await R.create_reward_for_order(db_session, o2.id) is None


async def test_min_order_amount_respected(db_session) -> None:
    await _set(db_session, referral_reward_value="5000",
               referral_reward_requires_admin_approval="false",
               referral_min_order_amount="200000")
    ref = await _user(db_session, 1)
    buyer = await _user(db_session, 2)
    await db_session.commit()
    code = await R.get_or_create_referral_code(db_session, ref.id)
    await db_session.commit()
    await R.register_referral(db_session, buyer.id, code)
    await db_session.commit()
    o = await _delivered_order(db_session, buyer.id, final=100_000)  # below 200k
    await db_session.commit()
    assert await R.create_reward_for_order(db_session, o.id) is None


async def test_zero_reward_value_tracks_but_no_payout(db_session) -> None:
    await _set(db_session, referral_reward_value="0")
    ref = await _user(db_session, 1)
    buyer = await _user(db_session, 2)
    await db_session.commit()
    code = await R.get_or_create_referral_code(db_session, ref.id)
    await db_session.commit()
    await R.register_referral(db_session, buyer.id, code)
    await db_session.commit()
    o = await _delivered_order(db_session, buyer.id)
    await db_session.commit()
    assert await R.create_reward_for_order(db_session, o.id) is None
    assert len(await R.list_rewards(db_session)) == 0

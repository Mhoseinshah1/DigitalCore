"""Referral invites + rewards (Phase 10).

A user shares `https://t.me/<bot>?start=ref_<code>`; the first /start carrying a
valid code attaches the referrer (once, never overwritten, never self). When a
referred user completes their first qualifying paid order,
`create_reward_for_order` mints a reward for the referrer — auto-paid to the
wallet, or left `pending` for admin approval, per settings. Everything is
idempotent: the `(order_id)` unique constraint + a referred-user row lock stop a
second reward, and the wallet payout is guarded by the reward status under a row
lock so it can never double-credit.
"""
from __future__ import annotations

import logging
import uuid
from datetime import datetime, timezone

from sqlalchemy import func, select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.settings_service import SettingsService
from app.models.order import Order
from app.models.referral_reward import ReferralReward
from app.models.user import User
from app.services import audit_service, wallet_service

log = logging.getLogger("referral")

REF_PREFIX = "ref_"


class ReferralError(ValueError):
    code = "referral_error"

    def __init__(self, message: str, code: str | None = None) -> None:
        super().__init__(message)
        if code is not None:
            self.code = code


def _now() -> datetime:
    return datetime.now(timezone.utc)


# --------------------------------------------------------------------------
# Settings
# --------------------------------------------------------------------------
async def referrals_enabled(session: AsyncSession) -> bool:
    return await SettingsService(session).get_bool("referrals_enabled", True)


async def _reward_enabled(session: AsyncSession) -> bool:
    return await SettingsService(session).get_bool("referral_reward_enabled", True)


async def _reward_type(session: AsyncSession) -> str:
    val = (await SettingsService(session).get_str("referral_reward_type", "fixed")).strip()
    return "percent" if val == "percent" else "fixed"


async def _reward_value(session: AsyncSession) -> int:
    return await SettingsService(session).get_int("referral_reward_value", 0)


async def _requires_approval(session: AsyncSession) -> bool:
    return await SettingsService(session).get_bool(
        "referral_reward_requires_admin_approval", False)


async def _first_order_only(session: AsyncSession) -> bool:
    return await SettingsService(session).get_bool("referral_reward_first_order_only", True)


async def _min_order_amount(session: AsyncSession) -> int:
    return await SettingsService(session).get_int("referral_min_order_amount", 0)


# --------------------------------------------------------------------------
# Codes
# --------------------------------------------------------------------------
def generate_referral_code(user_id: int) -> str:
    """A short, shareable, hard-to-guess code (not persisted here)."""
    return uuid.uuid4().hex[:8].upper()


async def get_or_create_referral_code(session: AsyncSession, user_id: int) -> str | None:
    user = await session.get(User, user_id)
    if user is None:
        return None
    if user.referral_code:
        return user.referral_code
    for _ in range(6):  # retry on the (astronomically rare) collision
        code = generate_referral_code(user_id)
        if await session.scalar(select(User.id).where(User.referral_code == code)) is not None:
            continue
        user.referral_code = code
        try:
            await session.flush()
        except IntegrityError:
            await session.rollback()
            user = await session.get(User, user_id)
            if user and user.referral_code:
                return user.referral_code
            continue
        await session.commit()
        return code
    return None


async def get_user_by_code(session: AsyncSession, code: str) -> User | None:
    code = (code or "").strip().upper()
    if not code:
        return None
    return await session.scalar(select(User).where(User.referral_code == code))


def parse_start_code(start_param: str | None) -> str | None:
    """Extract the referral code from a /start payload (``ref_<code>``)."""
    if not start_param:
        return None
    param = start_param.strip()
    if param.startswith(REF_PREFIX):
        return param[len(REF_PREFIX):].strip().upper() or None
    return None


# --------------------------------------------------------------------------
# Registration
# --------------------------------------------------------------------------
async def register_referral(
    session: AsyncSession, referred_user_id: int, referral_code: str
) -> User | None:
    """Attach a referrer to a user. Safe/idempotent: ignores an invalid code, a
    self-referral, or a user who already has a referrer. Returns the referrer."""
    if not await referrals_enabled(session):
        return None
    code = (referral_code or "").strip().upper()
    if not code:
        return None
    referred = await session.get(User, referred_user_id)
    if referred is None:
        return None
    if referred.referrer_id is not None:
        return None  # never overwrite an existing referrer
    referrer = await get_user_by_code(session, code)
    if referrer is None:
        return None  # invalid code — ignore silently
    if referrer.id == referred.id:
        return None  # cannot self-refer

    referred.referrer_id = referrer.id
    referred.referral_registered_at = _now()
    await audit_service.log(
        session, actor_type="user", actor_id=referred.id, action="referral_registered",
        target_type="user", target_id=referred.id,
        meta=f"referrer_id={referrer.id} code={code}",
    )
    await session.refresh(referred)
    return referrer


async def get_referrer(session: AsyncSession, user_id: int) -> User | None:
    user = await session.get(User, user_id)
    if user is None or user.referrer_id is None:
        return None
    return await session.get(User, user.referrer_id)


async def list_user_referrals(session: AsyncSession, user_id: int) -> list[User]:
    stmt = select(User).where(User.referrer_id == user_id).order_by(User.id.desc())
    return list((await session.execute(stmt)).scalars().all())


# --------------------------------------------------------------------------
# Rewards
# --------------------------------------------------------------------------
async def _has_prior_reward(session: AsyncSession, referred_user_id: int) -> bool:
    """True if the referred user already earned a referral reward (any order)."""
    return await session.scalar(
        select(ReferralReward.id).where(
            ReferralReward.referred_user_id == referred_user_id).limit(1)
    ) is not None


def _compute_amount(reward_type: str, reward_value: int, base_amount: int) -> int:
    if reward_type == "percent":
        return max(0, int(base_amount) * int(reward_value) // 100)
    return max(0, int(reward_value))


async def _pay_locked(session: AsyncSession, reward: ReferralReward) -> None:
    """Credit the referrer's wallet for an approved reward and mark it paid.
    Assumes `reward` is row-locked; idempotent via the status guard."""
    if reward.status == "paid":
        return
    user = await wallet_service._lock_user(session, reward.referrer_user_id)
    wallet_service._record_tx(
        session, user, int(reward.reward_amount), tx_type="reward", actor_type="system",
        actor_id=None, reason=f"referral reward #{reward.id}", order_id=reward.order_id)
    now = _now()
    if reward.approved_at is None:
        reward.approved_at = now
    reward.status = "paid"
    reward.paid_at = now
    wallet_service._audit_nocommit(
        session, "referral_reward_paid", actor_type="system", actor_id=None,
        target_type="referral_reward", target_id=reward.id,
        meta=f"referrer_id={reward.referrer_user_id} amount={reward.reward_amount}")


async def create_reward_for_order(
    session: AsyncSession, order_id: int, *, bot=None
) -> ReferralReward | None:
    """Create (and, unless approval is required, pay) the referral reward for a
    referred user's first qualifying paid order. Idempotent — never mints two."""
    if not await referrals_enabled(session) or not await _reward_enabled(session):
        return None
    order = await session.get(Order, order_id)
    if order is None or order.delivered_at is None:
        return None  # only a successfully delivered (paid) order qualifies

    # Serialize concurrent reward creation for the same referred user.
    referred = await session.scalar(
        select(User).where(User.id == order.user_id).with_for_update()
    )
    if referred is None or referred.referrer_id is None:
        return None
    if referred.referrer_id == referred.id:
        return None

    # Idempotency: a reward already exists for this exact order?
    if await session.scalar(
        select(ReferralReward.id).where(ReferralReward.order_id == order.id)
    ) is not None:
        return None

    if await _first_order_only(session):
        if await _has_prior_reward(session, referred.id):
            return None  # already rewarded on an earlier order

    base = int(order.final_amount or 0)
    if base < await _min_order_amount(session):
        return None
    amount = _compute_amount(await _reward_type(session), await _reward_value(session), base)
    if amount <= 0:
        return None  # referral tracked, but no payout configured

    reward = ReferralReward(
        referrer_user_id=referred.referrer_id, referred_user_id=referred.id,
        order_id=order.id, reward_type=await _reward_type(session),
        reward_amount=amount, status="pending",
    )
    session.add(reward)
    try:
        await session.flush()
    except IntegrityError:
        await session.rollback()
        return None  # lost the race — the other worker created it
    wallet_service._audit_nocommit(
        session, "referral_reward_created", actor_type="system", actor_id=None,
        target_type="referral_reward", target_id=reward.id,
        meta=f"referrer_id={reward.referrer_user_id} order={order.order_number} amount={amount}")

    if await _requires_approval(session):
        await session.commit()  # stays pending for an admin
        await _notify(bot, reward.referrer_user_id, session, "referral.notify.pending",
                      amount=amount)
        return reward

    # Auto path: approve + pay atomically.
    await _pay_locked(session, reward)
    await session.commit()
    await _notify(bot, reward.referrer_user_id, session, "referral.notify.paid", amount=amount)
    return reward


async def approve_reward(
    session: AsyncSession, reward_id: int, admin_id: int | None, *, bot=None
) -> ReferralReward:
    """Approve a pending reward and pay it to the referrer's wallet (atomic)."""
    reward = await session.scalar(
        select(ReferralReward).where(ReferralReward.id == reward_id).with_for_update()
    )
    if reward is None:
        raise ReferralError("reward not found", code="reward_not_found")
    if reward.status == "paid":
        return reward  # idempotent
    if reward.status not in ("pending", "approved"):
        raise ReferralError("reward is not payable", code="not_payable")
    wallet_service._audit_nocommit(
        session, "referral_reward_approved", actor_type="admin", actor_id=admin_id,
        target_type="referral_reward", target_id=reward.id)
    await _pay_locked(session, reward)
    await session.commit()
    await _notify(bot, reward.referrer_user_id, session, "referral.notify.paid",
                  amount=reward.reward_amount)
    return reward


async def pay_reward_to_wallet(
    session: AsyncSession, reward_id: int, *, admin_id: int | None = None, bot=None
) -> ReferralReward:
    """Pay an approved (or pending) reward to the referrer's wallet."""
    return await approve_reward(session, reward_id, admin_id, bot=bot)


async def reject_reward(
    session: AsyncSession, reward_id: int, admin_id: int | None, reason: str
) -> ReferralReward:
    if not (reason or "").strip():
        raise ReferralError("a reason is required", code="reason_required")
    reward = await session.scalar(
        select(ReferralReward).where(ReferralReward.id == reward_id).with_for_update()
    )
    if reward is None:
        raise ReferralError("reward not found", code="reward_not_found")
    if reward.status == "paid":
        raise ReferralError("reward was already paid", code="already_paid")
    reward.status = "rejected"
    reward.reject_reason = reason.strip()
    await audit_service.log(
        session, actor_type="admin", actor_id=admin_id, action="referral_reward_rejected",
        target_type="referral_reward", target_id=reward.id, meta=f"reason={reason.strip()}",
    )
    await session.refresh(reward)
    return reward


async def get_reward(session: AsyncSession, reward_id: int) -> ReferralReward | None:
    return await session.get(ReferralReward, reward_id)


async def list_rewards(
    session: AsyncSession, *, status: str | None = None, limit: int = 200, offset: int = 0
) -> list[ReferralReward]:
    stmt = select(ReferralReward)
    if status:
        stmt = stmt.where(ReferralReward.status == status)
    stmt = stmt.order_by(ReferralReward.id.desc()).limit(limit).offset(offset)
    return list((await session.execute(stmt)).scalars().all())


# --------------------------------------------------------------------------
# Stats (for the bot /referral page)
# --------------------------------------------------------------------------
async def referral_stats(session: AsyncSession, user_id: int) -> dict:
    invited = int(await session.scalar(
        select(func.count(User.id)).where(User.referrer_id == user_id)) or 0)
    paid_referrals = int(await session.scalar(
        select(func.count(func.distinct(ReferralReward.referred_user_id)))
        .where(ReferralReward.referrer_user_id == user_id)) or 0)
    total_rewards = int(await session.scalar(
        select(func.coalesce(func.sum(ReferralReward.reward_amount), 0))
        .where(ReferralReward.referrer_user_id == user_id,
               ReferralReward.status == "paid")) or 0)
    pending_rewards = int(await session.scalar(
        select(func.coalesce(func.sum(ReferralReward.reward_amount), 0))
        .where(ReferralReward.referrer_user_id == user_id,
               ReferralReward.status.in_(("pending", "approved")))) or 0)
    return {
        "invited": invited,
        "paid_referrals": paid_referrals,
        "total_rewards": total_rewards,
        "pending_rewards": pending_rewards,
    }


# --------------------------------------------------------------------------
# Best-effort referrer notification
# --------------------------------------------------------------------------
async def _notify(bot, user_id: int, session: AsyncSession, key: str, **params) -> None:
    user = await session.get(User, user_id)
    if user is None or not user.telegram_id:
        return
    from app.i18n import t
    lang = user.language if user.language else "fa"
    text = t(key, lang, **params)
    b, own = bot, None
    if b is None:
        from app.config import settings
        if not settings.TELEGRAM_BOT_TOKEN:
            return
        from aiogram import Bot
        own = b = Bot(settings.TELEGRAM_BOT_TOKEN)
    try:
        await b.send_message(user.telegram_id, text, parse_mode="HTML")
    except Exception as exc:  # noqa: BLE001 - notification is best-effort
        log.info("referral notify failed: %s", exc)
    finally:
        if own is not None:
            try:
                await own.session.close()
            except Exception:  # noqa: BLE001
                pass

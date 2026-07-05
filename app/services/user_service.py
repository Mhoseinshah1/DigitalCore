"""User registration and lifecycle. All bot handlers go through here."""
from __future__ import annotations

from datetime import datetime, timezone

from sqlalchemy import func, select, update
from sqlalchemy.ext.asyncio import AsyncSession

from app.i18n import SUPPORTED
from app.models.user import User
from app.models.wallet_transaction import WalletTransaction
from app.services import audit_service


def _now() -> datetime:
    return datetime.now(timezone.utc)


async def get_by_id(session: AsyncSession, user_id: int) -> User | None:
    return await session.get(User, user_id)


async def get_by_telegram_id(session: AsyncSession, telegram_id: int) -> User | None:
    result = await session.execute(select(User).where(User.telegram_id == telegram_id))
    return result.scalar_one_or_none()


async def register_or_update_user(
    session: AsyncSession,
    telegram_id: int,
    username: str | None = None,
    first_name: str | None = None,
    last_name: str | None = None,
    language_code: str | None = None,
) -> tuple[User, bool]:
    """Idempotent upsert by telegram_id.

    Returns (user, created). On create the join date comes from the created_at
    mixin default; on every call the profile fields and last_activity_at are
    refreshed.
    """
    user = await get_by_telegram_id(session, telegram_id)
    created = False
    if user is None:
        user = User(telegram_id=telegram_id)
        session.add(user)
        created = True

    user.username = username
    user.first_name = first_name
    user.last_name = last_name
    if language_code is not None:
        user.language_code = language_code
    user.last_activity_at = _now()
    await session.commit()
    await session.refresh(user)
    return user, created


# Phase 2 canonical name; delegates to the idempotent upsert above.
async def create_or_update_from_telegram(
    session: AsyncSession,
    *,
    telegram_id: int,
    username: str | None = None,
    first_name: str | None = None,
    last_name: str | None = None,
    language_code: str | None = None,
) -> tuple[User, bool]:
    return await register_or_update_user(
        session,
        telegram_id=telegram_id,
        username=username,
        first_name=first_name,
        last_name=last_name,
        language_code=language_code,
    )


async def touch_activity(session: AsyncSession, telegram_id: int) -> None:
    """Refresh last_activity_at for a known user; no-op for unknown ids."""
    await session.execute(
        update(User)
        .where(User.telegram_id == telegram_id)
        .values(last_activity_at=_now())
    )
    await session.commit()


async def set_language(session: AsyncSession, telegram_id: int, language: str) -> User | None:
    """Persist the user's UI language. Raises ValueError for unsupported codes."""
    if language not in SUPPORTED:
        raise ValueError(f"unsupported language: {language!r}")
    user = await get_by_telegram_id(session, telegram_id)
    if user is None:
        return None
    user.language = language
    await session.commit()
    return user


async def set_blocked(session: AsyncSession, telegram_id: int, blocked: bool) -> User | None:
    """Idempotently block/unblock a user. Returns the user or None if unknown."""
    user = await get_by_telegram_id(session, telegram_id)
    if user is None:
        return None
    user.is_blocked = blocked
    await session.commit()
    return user


async def block_user(session: AsyncSession, telegram_id: int) -> User | None:
    return await set_blocked(session, telegram_id, True)


async def unblock_user(session: AsyncSession, telegram_id: int) -> User | None:
    return await set_blocked(session, telegram_id, False)


# --------------------------------------------------------------------------
# Listing (admin panel)
# --------------------------------------------------------------------------
async def list_users(
    session: AsyncSession,
    *,
    blocked: bool | None = None,
    search: str | None = None,
    limit: int = 200,
) -> list[User]:
    """Users ordered newest-first, optionally filtered by blocked state/search."""
    stmt = select(User)
    if blocked is not None:
        stmt = stmt.where(User.is_blocked.is_(blocked))
    if search:
        like = f"%{search.strip()}%"
        conditions = [User.username.ilike(like), User.first_name.ilike(like), User.last_name.ilike(like)]
        digits = search.strip().lstrip("@")
        if digits.isdigit():
            conditions.append(User.telegram_id == int(digits))
        from sqlalchemy import or_

        stmt = stmt.where(or_(*conditions))
    stmt = stmt.order_by(User.id.desc()).limit(limit)
    result = await session.execute(stmt)
    return list(result.scalars().all())


async def list_blocked_users(session: AsyncSession, *, limit: int = 200) -> list[User]:
    return await list_users(session, blocked=True, limit=limit)


async def get_stats(session: AsyncSession) -> dict[str, int]:
    """Counts for the admin dashboard."""
    total = await session.scalar(select(func.count(User.id))) or 0
    blocked = await session.scalar(
        select(func.count(User.id)).where(User.is_blocked.is_(True))
    ) or 0
    verified = await session.scalar(
        select(func.count(User.id)).where(User.is_verified.is_(True))
    ) or 0
    return {
        "total_users": int(total),
        "blocked_users": int(blocked),
        "verified_users": int(verified),
    }


def get_user_summary(user: User) -> dict[str, object]:
    """A flat, template-friendly summary of a user's key fields."""
    full_name = " ".join(p for p in (user.first_name, user.last_name) if p) or None
    return {
        "id": user.id,
        "telegram_id": user.telegram_id,
        "username": user.username,
        "full_name": full_name,
        "phone_number": user.phone_number,
        "wallet_balance": int(user.wallet_balance or 0),
        "is_blocked": user.is_blocked,
        "is_verified": user.is_verified,
        "language": user.language,
        "language_code": user.language_code,
        "admin_note": user.admin_note,
        "created_at": user.created_at,
        "last_activity_at": user.last_activity_at,
    }


# --------------------------------------------------------------------------
# Admin actions (by user_id, audited)
# --------------------------------------------------------------------------
async def admin_set_blocked(
    session: AsyncSession,
    user_id: int,
    blocked: bool,
    *,
    actor_id: int | None = None,
    ip_address: str | None = None,
) -> User | None:
    """Block/unblock by primary key, writing an audit row. Idempotent."""
    user = await get_by_id(session, user_id)
    if user is None:
        return None
    if user.is_blocked == blocked:
        return user
    user.is_blocked = blocked
    await audit_service.log(
        session,
        actor_type="admin",
        actor_id=actor_id,
        action="user.blocked" if blocked else "user.unblocked",
        target_type="user",
        target_id=user.id,
        old=str(not blocked),
        new=str(blocked),
        ip_address=ip_address,
    )
    return user


async def set_restricted(
    session: AsyncSession,
    user_id: int,
    restricted: bool,
    *,
    reason: str | None = None,
    restricted_until=None,
    actor_id: int | None = None,
    ip_address: str | None = None,
) -> User | None:
    """Restrict/unrestrict a user (softer than a block). Writes an audit row."""
    user = await get_by_id(session, user_id)
    if user is None:
        return None
    user.is_restricted = restricted
    user.restriction_reason = (reason or None) if restricted else None
    user.restricted_until = restricted_until if restricted else None
    await audit_service.log(
        session,
        actor_type="admin",
        actor_id=actor_id,
        action="user.restricted" if restricted else "user.unrestricted",
        target_type="user",
        target_id=user.id,
        new=(reason or None) if restricted else None,
        ip_address=ip_address,
    )
    return user


async def set_verified(
    session: AsyncSession,
    user_id: int,
    verified: bool,
    *,
    actor_id: int | None = None,
    ip_address: str | None = None,
) -> User | None:
    user = await get_by_id(session, user_id)
    if user is None:
        return None
    if user.is_verified == verified:
        return user
    user.is_verified = verified
    await audit_service.log(
        session,
        actor_type="admin",
        actor_id=actor_id,
        action="user.verified" if verified else "user.unverified",
        target_type="user",
        target_id=user.id,
        old=str(not verified),
        new=str(verified),
        ip_address=ip_address,
    )
    return user


async def update_admin_note(
    session: AsyncSession,
    user_id: int,
    note: str | None,
    *,
    actor_id: int | None = None,
    ip_address: str | None = None,
) -> User | None:
    user = await get_by_id(session, user_id)
    if user is None:
        return None
    cleaned = (note or "").strip() or None
    if cleaned == user.admin_note:
        return user
    user.admin_note = cleaned
    await audit_service.log(
        session,
        actor_type="admin",
        actor_id=actor_id,
        action="user.note_updated",
        target_type="user",
        target_id=user.id,
        ip_address=ip_address,
    )
    return user


async def adjust_wallet_balance(
    session: AsyncSession,
    user_id: int,
    amount: int,
    *,
    reason: str | None = None,
    actor_type: str = "admin",
    actor_id: int | None = None,
    allow_negative: bool = False,
    transaction_type: str = "admin_adjustment",
    ip_address: str | None = None,
) -> User:
    """Credit (+) or debit (-) a user's wallet.

    Records a WalletTransaction and an audit row for every change. Raises
    ValueError when the user is missing, the amount is zero, or the result would
    be negative and `allow_negative` is False.
    """
    amount = int(amount)
    if amount == 0:
        raise ValueError("amount must be non-zero")
    user = await get_by_id(session, user_id)
    if user is None:
        raise ValueError("user not found")

    old_balance = int(user.wallet_balance or 0)
    new_balance = old_balance + amount
    if new_balance < 0 and not allow_negative:
        raise ValueError("wallet balance cannot go negative")

    user.wallet_balance = new_balance
    session.add(
        WalletTransaction(
            user_id=user.id,
            amount=amount,
            balance_before=old_balance,
            balance_after=new_balance,
            type=transaction_type,
            reason=(reason or "").strip() or None,
            actor_type=actor_type,
            actor_id=actor_id,
        )
    )
    await session.flush()
    await audit_service.log(
        session,
        actor_type=actor_type,
        actor_id=actor_id,
        action="wallet.adjusted",
        target_type="user",
        target_id=user.id,
        old=str(old_balance),
        new=str(new_balance),
        meta=(reason or None),
        ip_address=ip_address,
    )
    await session.refresh(user)
    return user


async def list_wallet_transactions(
    session: AsyncSession, *, user_id: int | None = None, limit: int = 200
) -> list[WalletTransaction]:
    stmt = select(WalletTransaction)
    if user_id is not None:
        stmt = stmt.where(WalletTransaction.user_id == user_id)
    stmt = stmt.order_by(WalletTransaction.id.desc()).limit(limit)
    result = await session.execute(stmt)
    return list(result.scalars().all())

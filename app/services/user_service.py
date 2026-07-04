"""User registration and lifecycle. All bot handlers go through here."""
from __future__ import annotations

from datetime import datetime, timezone

from sqlalchemy import select, update
from sqlalchemy.ext.asyncio import AsyncSession

from app.models.user import User


def _now() -> datetime:
    return datetime.now(timezone.utc)


async def get_by_telegram_id(session: AsyncSession, telegram_id: int) -> User | None:
    result = await session.execute(select(User).where(User.telegram_id == telegram_id))
    return result.scalar_one_or_none()


async def register_or_update_user(
    session: AsyncSession,
    telegram_id: int,
    username: str | None = None,
    first_name: str | None = None,
    last_name: str | None = None,
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
    user.last_activity_at = _now()
    await session.commit()
    await session.refresh(user)
    return user, created


async def touch_activity(session: AsyncSession, telegram_id: int) -> None:
    """Refresh last_activity_at for a known user; no-op for unknown ids."""
    await session.execute(
        update(User)
        .where(User.telegram_id == telegram_id)
        .values(last_activity_at=_now())
    )
    await session.commit()


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

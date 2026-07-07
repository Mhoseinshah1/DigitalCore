"""Bot-side admin recognition.

The main admin is settings.TELEGRAM_ADMIN_ID and is ALWAYS treated as owner,
even without a row in the admins table. The admins table itself is the web
panel's (username-based) identity and has no Telegram linkage yet; when
admin↔Telegram linking lands in a later phase, get_role() grows a DB lookup —
the session parameter is already part of the signature for that reason.
"""
from __future__ import annotations

from sqlalchemy.ext.asyncio import AsyncSession

from app.config import settings
from app.core.permissions import Role


async def get_role(session: AsyncSession, telegram_id: int) -> Role | None:
    """The RBAC role for a Telegram account, or None for regular users."""
    if settings.TELEGRAM_ADMIN_ID and telegram_id == settings.TELEGRAM_ADMIN_ID:
        return Role.OWNER
    # Later phase: look up a linked admin row and return its .role.
    return None


async def is_admin(session: AsyncSession, telegram_id: int) -> bool:
    return (await get_role(session, telegram_id)) is not None


async def resolve_admin_id(session: AsyncSession, telegram_id: int) -> int | None:
    """A best-effort ``admins.id`` to attribute a Telegram admin's action to.

    Telegram↔admin linking doesn't exist yet, so a recognised Telegram admin
    (the bootstrap owner) is attributed to the first super-admin row if one
    exists. Returns None when the account isn't an admin or no admin row exists
    (e.g. in tests) — callers treat None as "unattributed".
    """
    if await get_role(session, telegram_id) is None:
        return None
    from sqlalchemy import select

    from app.models.admin import Admin
    admin = await session.scalar(
        select(Admin).where(Admin.is_super_admin.is_(True)).order_by(Admin.id).limit(1)
    )
    return admin.id if admin else None

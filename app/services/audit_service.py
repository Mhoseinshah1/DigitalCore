"""Audit logging: every notable state change writes an AuditLog row."""
from __future__ import annotations

from typing import Any

from sqlalchemy.ext.asyncio import AsyncSession

from app.models.audit_log import AuditLog

ACTOR_TYPES = {"user", "admin", "system"}


async def log(
    session: AsyncSession,
    actor_type: str,
    actor_id: int | None,
    action: str,
    target_type: str | None = None,
    target_id: Any | None = None,
    old: Any | None = None,
    new: Any | None = None,
) -> AuditLog:
    """Write one audit row. Values are stringified; never pass secrets here."""
    if actor_type not in ACTOR_TYPES:
        raise ValueError(f"actor_type must be one of {sorted(ACTOR_TYPES)}")
    row = AuditLog(
        actor_type=actor_type,
        actor_id=actor_id,
        action=action,
        target_type=target_type,
        target_id=None if target_id is None else str(target_id),
        old_value=None if old is None else str(old),
        new_value=None if new is None else str(new),
    )
    session.add(row)
    await session.commit()
    return row

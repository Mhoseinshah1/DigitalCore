"""Audit logging: every notable state change writes an AuditLog row."""
from __future__ import annotations

from typing import Any

from sqlalchemy import select
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
    meta: Any | None = None,
    ip_address: str | None = None,
) -> AuditLog:
    """Write one audit row. Values are stringified; never pass secrets here.

    `meta` is optional free-form context (stringified) and `ip_address` the
    source IP for web-initiated actions.
    """
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
        meta=None if meta is None else str(meta),
        ip_address=ip_address,
    )
    session.add(row)
    await session.commit()
    return row


async def list_recent(
    session: AsyncSession, *, action: str | None = None, limit: int = 200
) -> list[AuditLog]:
    """Most-recent audit rows first, optionally filtered by action prefix."""
    stmt = select(AuditLog)
    if action:
        stmt = stmt.where(AuditLog.action.like(f"{action}%"))
    stmt = stmt.order_by(AuditLog.id.desc()).limit(limit)
    result = await session.execute(stmt)
    return list(result.scalars().all())

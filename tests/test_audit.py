"""audit_service and the /start service-level flow (no live Telegram needed)."""
from __future__ import annotations

import pytest
from sqlalchemy import func, select

from app.models import AuditLog, User
from app.services import audit_service, user_service


async def test_log_writes_a_row(db_session) -> None:
    row = await audit_service.log(
        db_session,
        actor_type="system",
        actor_id=None,
        action="test.action",
        target_type="setting",
        target_id=42,
        old="off",
        new="on",
    )
    assert row.id is not None
    stored = (await db_session.execute(select(AuditLog))).scalars().one()
    assert stored.action == "test.action"
    assert stored.actor_type == "system"
    assert stored.target_id == "42"  # stringified
    assert stored.old_value == "off" and stored.new_value == "on"
    assert stored.created_at is not None


async def test_invalid_actor_type_rejected(db_session) -> None:
    with pytest.raises(ValueError):
        await audit_service.log(db_session, actor_type="alien", actor_id=None, action="x")


async def test_start_flow_creates_user_and_audit_row(db_session) -> None:
    """Mirrors what handlers/user/start.py does on first /start."""
    user, created = await user_service.register_or_update_user(
        db_session, telegram_id=555001, username="newbie", first_name="New", last_name=None
    )
    assert created
    await audit_service.log(
        db_session,
        actor_type="user",
        actor_id=555001,
        action="user.registered",
        target_type="user",
        target_id=user.id,
    )

    users = (await db_session.execute(select(func.count()).select_from(User))).scalar_one()
    audits = (
        await db_session.execute(
            select(func.count()).select_from(AuditLog).where(AuditLog.action == "user.registered")
        )
    ).scalar_one()
    assert users == 1
    assert audits == 1

    # Second /start: no new user, and the handler logs nothing (created=False).
    _, created_again = await user_service.register_or_update_user(
        db_session, telegram_id=555001, username="newbie"
    )
    assert created_again is False
    users = (await db_session.execute(select(func.count()).select_from(User))).scalar_one()
    assert users == 1

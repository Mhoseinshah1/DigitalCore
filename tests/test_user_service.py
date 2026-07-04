"""user_service: idempotent registration and block/unblock."""
from __future__ import annotations

from sqlalchemy import func, select

from app.models import User
from app.services import user_service


async def test_register_is_idempotent_and_updates_fields(db_session) -> None:
    user1, created1 = await user_service.register_or_update_user(
        db_session, telegram_id=1001, username="alice", first_name="Alice", last_name=None
    )
    assert created1 is True
    assert user1.id is not None
    assert user1.last_activity_at is not None
    assert user1.created_at is not None  # join date set on create

    user2, created2 = await user_service.register_or_update_user(
        db_session, telegram_id=1001, username="alice2", first_name="Alice", last_name="Smith"
    )
    assert created2 is False
    assert user2.id == user1.id
    assert user2.username == "alice2"
    assert user2.last_name == "Smith"

    count = (await db_session.execute(select(func.count()).select_from(User))).scalar_one()
    assert count == 1


async def test_get_by_telegram_id(db_session) -> None:
    await user_service.register_or_update_user(db_session, telegram_id=1002, username="bob")
    found = await user_service.get_by_telegram_id(db_session, 1002)
    assert found is not None and found.username == "bob"
    assert await user_service.get_by_telegram_id(db_session, 999999) is None


async def test_language_defaults_to_fa_and_persists(db_session) -> None:
    from app.i18n import t

    user, _created = await user_service.register_or_update_user(db_session, telegram_id=1010)
    assert user.language == "fa"
    assert t("greeting", user.language) == t("greeting", "fa")

    updated = await user_service.set_language(db_session, 1010, "en")
    assert updated is not None and updated.language == "en"
    refetched = await user_service.get_by_telegram_id(db_session, 1010)
    assert refetched is not None and refetched.language == "en"
    # Bot output switches with the stored language.
    assert t("greeting", refetched.language) == "👋 Welcome to DigitalCore!"


async def test_set_language_rejects_unsupported(db_session) -> None:
    import pytest as _pytest

    await user_service.register_or_update_user(db_session, telegram_id=1011)
    with _pytest.raises(ValueError):
        await user_service.set_language(db_session, 1011, "de")
    assert await user_service.set_language(db_session, 999999, "en") is None


async def test_block_unblock(db_session) -> None:
    await user_service.register_or_update_user(db_session, telegram_id=1003)
    blocked = await user_service.block_user(db_session, 1003)
    assert blocked is not None and blocked.is_blocked is True
    # Idempotent: blocking again keeps the same state.
    blocked_again = await user_service.block_user(db_session, 1003)
    assert blocked_again is not None and blocked_again.is_blocked is True
    unblocked = await user_service.unblock_user(db_session, 1003)
    assert unblocked is not None and unblocked.is_blocked is False
    assert await user_service.block_user(db_session, 424242) is None

"""admin_service: the env main admin is always owner; everyone else is a user."""
from __future__ import annotations

from app.config import settings
from app.core.permissions import Role
from app.services import admin_service


async def test_env_main_admin_is_owner(db_session, monkeypatch) -> None:
    monkeypatch.setattr(settings, "TELEGRAM_ADMIN_ID", 777001)
    assert await admin_service.is_admin(db_session, 777001) is True
    assert await admin_service.get_role(db_session, 777001) is Role.OWNER


async def test_other_ids_are_not_admins(db_session, monkeypatch) -> None:
    monkeypatch.setattr(settings, "TELEGRAM_ADMIN_ID", 777001)
    assert await admin_service.is_admin(db_session, 123) is False
    assert await admin_service.get_role(db_session, 123) is None


async def test_no_env_admin_configured(db_session, monkeypatch) -> None:
    monkeypatch.setattr(settings, "TELEGRAM_ADMIN_ID", None)
    assert await admin_service.is_admin(db_session, 777001) is False

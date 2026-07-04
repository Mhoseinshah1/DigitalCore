"""authenticate_admin: the single credential check for JSON and HTML login.

Admins sign in with their username; the optional email also works as the
identifier when set.
"""
from __future__ import annotations

from app.core.security import hash_password
from app.models import Admin
from app.web.api.auth import authenticate_admin

USERNAME = "owner"
EMAIL = "owner@example.com"
PASSWORD = "s3cure-pass"


async def _add_admin(session, *, active: bool = True, email: str | None = EMAIL) -> Admin:
    admin = Admin(
        username=USERNAME,
        email=email,
        password_hash=hash_password(PASSWORD),
        is_active=active,
        is_super_admin=True,
    )
    session.add(admin)
    await session.commit()
    return admin


async def test_login_by_username(db_session) -> None:
    await _add_admin(db_session)
    admin = await authenticate_admin(db_session, USERNAME, PASSWORD)
    assert admin is not None
    assert admin.username == USERNAME


async def test_login_by_email(db_session) -> None:
    await _add_admin(db_session)
    admin = await authenticate_admin(db_session, EMAIL, PASSWORD)
    assert admin is not None
    assert admin.username == USERNAME


async def test_wrong_password_returns_none(db_session) -> None:
    await _add_admin(db_session)
    assert await authenticate_admin(db_session, USERNAME, "wrong") is None


async def test_unknown_identifier_returns_none(db_session) -> None:
    await _add_admin(db_session)
    assert await authenticate_admin(db_session, "nobody", PASSWORD) is None


async def test_inactive_admin_returns_none(db_session) -> None:
    await _add_admin(db_session, active=False)
    assert await authenticate_admin(db_session, USERNAME, PASSWORD) is None


async def test_admin_without_email_logs_in_by_username(db_session) -> None:
    await _add_admin(db_session, email=None)
    admin = await authenticate_admin(db_session, USERNAME, PASSWORD)
    assert admin is not None


async def test_identifier_is_trimmed(db_session) -> None:
    await _add_admin(db_session)
    assert await authenticate_admin(db_session, f"  {USERNAME}  ", PASSWORD) is not None


async def test_blank_identifier_returns_none(db_session) -> None:
    await _add_admin(db_session)
    assert await authenticate_admin(db_session, "", PASSWORD) is None

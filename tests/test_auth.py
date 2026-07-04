"""authenticate_admin: the single credential check for JSON and HTML login."""
from __future__ import annotations

from app.core.security import hash_password
from app.models import Admin
from app.web.api.auth import authenticate_admin

EMAIL = "owner@example.com"
PASSWORD = "s3cure-pass"


async def _add_admin(session, *, active: bool = True) -> Admin:
    admin = Admin(
        email=EMAIL,
        password_hash=hash_password(PASSWORD),
        is_active=active,
        is_super_admin=True,
    )
    session.add(admin)
    await session.commit()
    return admin


async def test_correct_email_and_password_returns_admin(db_session) -> None:
    await _add_admin(db_session)
    admin = await authenticate_admin(db_session, EMAIL, PASSWORD)
    assert admin is not None
    assert admin.email == EMAIL


async def test_wrong_password_returns_none(db_session) -> None:
    await _add_admin(db_session)
    assert await authenticate_admin(db_session, EMAIL, "wrong") is None


async def test_unknown_email_returns_none(db_session) -> None:
    await _add_admin(db_session)
    assert await authenticate_admin(db_session, "nobody@example.com", PASSWORD) is None


async def test_inactive_admin_returns_none(db_session) -> None:
    await _add_admin(db_session, active=False)
    assert await authenticate_admin(db_session, EMAIL, PASSWORD) is None


async def test_email_is_trimmed(db_session) -> None:
    await _add_admin(db_session)
    admin = await authenticate_admin(db_session, f"  {EMAIL}  ", PASSWORD)
    assert admin is not None

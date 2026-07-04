"""Auth dependency: resolve the current admin from a Bearer JWT."""
from __future__ import annotations

from fastapi import Depends, HTTPException, Request, status
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.security import decode_access_token
from app.database import get_session
from app.models.admin import Admin


def _extract_token(request: Request) -> str | None:
    auth = request.headers.get("Authorization", "")
    if auth.lower().startswith("bearer "):
        return auth[7:].strip()
    return None


async def get_current_admin(
    request: Request,
    session: AsyncSession = Depends(get_session),
) -> Admin:
    """Return the authenticated admin or raise 401."""
    credentials_error = HTTPException(
        status.HTTP_401_UNAUTHORIZED,
        detail="Not authenticated",
        headers={"WWW-Authenticate": "Bearer"},
    )
    token = _extract_token(request)
    if not token:
        raise credentials_error
    payload = decode_access_token(token)
    if not payload:
        raise credentials_error
    sub = payload.get("sub")
    if sub is None or not str(sub).isdigit():
        raise credentials_error
    admin = await session.get(Admin, int(sub))
    if admin is None or not admin.is_active:
        raise credentials_error
    return admin

"""Auth dependencies: resolve the current admin from a Bearer JWT or the panel
session cookie.

The JSON API uses get_current_admin (raises 401). The server-rendered panel
pages use get_current_admin_optional (returns None) so they can redirect to
/login instead of erroring.
"""
from __future__ import annotations

from fastapi import Depends, HTTPException, Request, Response, status
from sqlalchemy.ext.asyncio import AsyncSession

from app.config import settings
from app.core.security import decode_access_token
from app.database import get_session
from app.models.admin import Admin

COOKIE_NAME = "dc_session"


def set_session_cookie(
    response: Response, token: str, *, request: Request | None = None
) -> None:
    """Attach the panel session cookie with consistent, safe attributes.

    Secure is decided by the COOKIE_SECURE setting: "true"/"false" force it;
    "auto" (default) sets it only when the request ACTUALLY arrived over HTTPS.
    uvicorn runs with --proxy-headers/--forwarded-allow-ips='*', so
    request.url.scheme is "https" exactly when a TLS-terminating proxy forwards
    X-Forwarded-Proto=https; a panel served over plain http gets a non-Secure
    cookie and login keeps working. The lifetime tracks the JWT expiry.
    """
    mode = (settings.COOKIE_SECURE or "auto").strip().lower()
    if mode == "true":
        secure = True
    elif mode == "false":
        secure = False
    else:  # auto
        secure = request is not None and request.url.scheme == "https"
    response.set_cookie(
        COOKIE_NAME,
        token,
        httponly=True,
        samesite="lax",
        secure=secure,
        max_age=settings.JWT_EXPIRE_MINUTES * 60,
    )


def _extract_token(request: Request) -> str | None:
    auth = request.headers.get("Authorization", "")
    if auth.lower().startswith("bearer "):
        return auth[7:].strip()
    return request.cookies.get(COOKIE_NAME)


async def _resolve_admin(request: Request, session: AsyncSession) -> Admin | None:
    token = _extract_token(request)
    if not token:
        return None
    payload = decode_access_token(token)
    if not payload:
        return None
    sub = payload.get("sub")
    if sub is None or not str(sub).isdigit():
        return None
    admin = await session.get(Admin, int(sub))
    if admin is None or not admin.is_active:
        return None
    return admin


async def get_current_admin(
    request: Request,
    session: AsyncSession = Depends(get_session),
) -> Admin:
    """Return the authenticated admin or raise 401 (JSON API dependency)."""
    admin = await _resolve_admin(request, session)
    if admin is None:
        raise HTTPException(
            status.HTTP_401_UNAUTHORIZED,
            detail="Not authenticated",
            headers={"WWW-Authenticate": "Bearer"},
        )
    return admin


async def get_current_admin_optional(
    request: Request,
    session: AsyncSession = Depends(get_session),
) -> Admin | None:
    """Return the authenticated admin or None (panel page dependency)."""
    return await _resolve_admin(request, session)

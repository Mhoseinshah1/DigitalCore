"""Authentication dependencies for the web panel."""
from __future__ import annotations

from fastapi import Depends, Request, Response
from sqlalchemy.ext.asyncio import AsyncSession

from app.config import settings
from app.core.security import decode_access_token
from app.database import get_session
from app.models.admin import Admin

COOKIE_NAME = "dc_session"


def set_session_cookie(response: Response, token: str) -> None:
    """Attach the session cookie with consistent, safe attributes.

    Secure is set when the panel is served over HTTPS (derived from WEB_PANEL_URL)
    so real deployments never leak the token over plaintext, while local http
    development still works. The lifetime tracks the JWT expiry.
    """
    response.set_cookie(
        COOKIE_NAME,
        token,
        httponly=True,
        samesite="lax",
        secure=settings.cookie_secure,
        max_age=settings.cookie_max_age,
    )


def _extract_token(request: Request) -> str | None:
    token = request.cookies.get(COOKIE_NAME)
    if token:
        return token
    auth = request.headers.get("Authorization", "")
    if auth.lower().startswith("bearer "):
        return auth[7:].strip()
    return None


async def current_admin(
    request: Request,
    session: AsyncSession = Depends(get_session),
) -> Admin | None:
    """Return the signed-in admin, or None. Never raises (page views branch on it)."""
    token = _extract_token(request)
    if not token:
        return None
    payload = decode_access_token(token)
    if not payload:
        return None
    sub = payload.get("sub")
    if sub is None:
        return None
    admin = await session.get(Admin, int(sub)) if str(sub).isdigit() else None
    if admin is None or not admin.is_active:
        return None
    return admin

"""JSON auth API for the web panel."""
from __future__ import annotations

from fastapi import APIRouter, Depends, HTTPException, Response, status
from pydantic import BaseModel
from sqlalchemy import or_, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.security import create_access_token, verify_password
from app.database import get_session
from app.models.admin import Admin
from app.web.deps import COOKIE_NAME, current_admin, set_session_cookie

router = APIRouter(prefix="/api/auth", tags=["auth"])


class LoginRequest(BaseModel):
    username: str
    password: str


class TokenResponse(BaseModel):
    access_token: str
    token_type: str = "bearer"


async def _authenticate(session: AsyncSession, username: str, password: str) -> Admin | None:
    username = (username or "").strip()
    conditions = [Admin.username == username]
    if username.isdigit():
        conditions.append(Admin.telegram_id == int(username))
    result = await session.execute(select(Admin).where(or_(*conditions)))
    admin = result.scalars().first()
    if admin is None or not admin.is_active:
        return None
    if not verify_password(password, admin.password_hash):
        return None
    return admin


@router.post("/login", response_model=TokenResponse)
async def login(
    body: LoginRequest,
    response: Response,
    session: AsyncSession = Depends(get_session),
) -> TokenResponse:
    admin = await _authenticate(session, body.username, body.password)
    if admin is None:
        raise HTTPException(status.HTTP_401_UNAUTHORIZED, "Invalid credentials")
    token = create_access_token(admin.id, is_owner=admin.is_owner)
    set_session_cookie(response, token)
    return TokenResponse(access_token=token)


@router.post("/logout")
async def logout(response: Response) -> dict[str, bool]:
    response.delete_cookie(COOKIE_NAME)
    return {"ok": True}


@router.get("/me")
async def me(admin: Admin | None = Depends(current_admin)) -> dict:
    if admin is None:
        raise HTTPException(status.HTTP_401_UNAUTHORIZED, "Not authenticated")
    return {
        "id": admin.id,
        "telegram_id": admin.telegram_id,
        "username": admin.username,
        "is_owner": admin.is_owner,
    }

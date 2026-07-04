"""Admin authentication API (email + password, JWT bearer)."""
from __future__ import annotations

from fastapi import APIRouter, Depends, HTTPException, status
from pydantic import BaseModel, EmailStr
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.security import create_access_token, verify_password
from app.database import get_session
from app.models.admin import Admin
from app.web.deps import get_current_admin

router = APIRouter(prefix="/api/auth", tags=["auth"])


class LoginRequest(BaseModel):
    email: EmailStr
    password: str


class TokenResponse(BaseModel):
    access_token: str
    token_type: str = "bearer"


class AdminOut(BaseModel):
    id: int
    email: EmailStr
    is_active: bool
    is_super_admin: bool


async def authenticate_admin(
    session: AsyncSession, email: str, password: str
) -> Admin | None:
    """Check email + password against the admins table; None on any failure.

    Shared by the JSON login endpoint and the panel's HTML login form.
    """
    email = (email or "").strip()
    result = await session.execute(select(Admin).where(Admin.email == email))
    admin = result.scalar_one_or_none()
    if admin is None or not admin.is_active or not verify_password(password, admin.password_hash):
        return None
    return admin


@router.post("/login", response_model=TokenResponse)
async def login(
    body: LoginRequest,
    session: AsyncSession = Depends(get_session),
) -> TokenResponse:
    admin = await authenticate_admin(session, str(body.email), body.password)
    if admin is None:
        raise HTTPException(status.HTTP_401_UNAUTHORIZED, "Invalid email or password")
    token = create_access_token(admin.id, email=admin.email)
    return TokenResponse(access_token=token)


@router.get("/me", response_model=AdminOut)
async def me(admin: Admin = Depends(get_current_admin)) -> AdminOut:
    return AdminOut(
        id=admin.id,
        email=admin.email,
        is_active=admin.is_active,
        is_super_admin=admin.is_super_admin,
    )

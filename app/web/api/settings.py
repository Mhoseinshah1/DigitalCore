"""JSON settings API for the web panel.

All business settings are read and written here. The installer never touches
these — they exist so the owner can configure the business after boot.
"""
from __future__ import annotations

from typing import Any

from fastapi import APIRouter, Depends, HTTPException, status
from pydantic import BaseModel
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.defaults import CATEGORIES, DEFAULTS_BY_KEY
from app.core.settings_service import SettingsService, coerce_out
from app.core import crypto
from app.database import get_session
from app.models.admin import Admin
from app.web.deps import current_admin

router = APIRouter(prefix="/api/settings", tags=["settings"])


def _require_admin(admin: Admin | None) -> Admin:
    if admin is None:
        raise HTTPException(status.HTTP_401_UNAUTHORIZED, "Not authenticated")
    return admin


class UpdateRequest(BaseModel):
    values: dict[str, Any]


@router.get("")
async def list_settings(
    admin: Admin | None = Depends(current_admin),
    session: AsyncSession = Depends(get_session),
) -> dict[str, Any]:
    _require_admin(admin)
    svc = SettingsService(session)
    rows = await svc.all_rows()

    grouped: dict[str, dict[str, Any]] = {}
    for cat, meta in sorted(CATEGORIES.items(), key=lambda kv: kv[1]["order"]):
        grouped[cat] = {**meta, "category": cat, "items": []}

    for row in sorted(rows, key=lambda r: r.key):
        if row.is_secret:
            display: Any = "" if not row.value else "********"
        else:
            display = coerce_out(row.value_type, row.value)
        entry = {
            "key": row.key,
            "value": display,
            "value_type": row.value_type,
            "is_secret": row.is_secret,
            "label": row.label,
            "description": row.description,
        }
        grouped.setdefault(
            row.category,
            {"title": row.category.title(), "order": 99, "category": row.category, "items": []},
        )["items"].append(entry)

    return {"categories": [g for g in grouped.values() if g["items"]]}


@router.put("")
async def update_settings(
    body: UpdateRequest,
    admin: Admin | None = Depends(current_admin),
    session: AsyncSession = Depends(get_session),
) -> dict[str, Any]:
    _require_admin(admin)
    unknown = [k for k in body.values if k not in DEFAULTS_BY_KEY]
    if unknown:
        raise HTTPException(
            status.HTTP_400_BAD_REQUEST, f"Unknown setting keys: {', '.join(unknown)}"
        )
    svc = SettingsService(session)
    await svc.update_many(body.values)
    return {"ok": True, "updated": [k for k in body.values]}

"""JSON settings API for the web panel.

All business settings are read and written here. The installer never touches
these — they exist so the owner can configure the business after boot. Display
metadata (category, type, label) comes from the code catalog in
app/core/defaults.py; the database stores only key/value/is_secret.
"""
from __future__ import annotations

from typing import Any

from fastapi import APIRouter, Depends, HTTPException, status
from pydantic import BaseModel
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.defaults import CATEGORIES, DEFAULTS, DEFAULTS_BY_KEY
from app.core.settings_service import SettingsService, coerce_out
from app.database import get_session
from app.models.admin import Admin
from app.web.deps import get_current_admin

router = APIRouter(prefix="/api/settings", tags=["settings"])


class UpdateRequest(BaseModel):
    values: dict[str, Any]


@router.get("")
async def list_settings(
    admin: Admin = Depends(get_current_admin),
    session: AsyncSession = Depends(get_session),
) -> dict[str, Any]:
    svc = SettingsService(session)
    rows = {r.key: r for r in await svc.all_rows()}

    grouped: dict[str, dict[str, Any]] = {}
    for cat, meta in sorted(CATEGORIES.items(), key=lambda kv: kv[1]["order"]):
        grouped[cat] = {**meta, "category": cat, "items": []}

    # Catalog first, overlaying stored rows — a fresh install reports the same
    # defaults the HTML settings page shows instead of an empty list.
    for d in DEFAULTS:
        row = rows.pop(d.key, None)
        if d.is_secret or (row is not None and row.is_secret):
            has_value = bool(row and row.value)
            display: Any = "********" if has_value else ""
        elif row is None:
            display = coerce_out(d.value_type, d.default)
        else:
            display = coerce_out(d.value_type, row.value)
        entry = {
            "key": d.key,
            "value": display,
            "value_type": d.value_type,
            "is_secret": d.is_secret,
            "label": d.label or d.key,
            "description": d.description,
        }
        grouped.setdefault(
            d.category,
            {"title": d.category.title(), "order": 99, "category": d.category, "items": []},
        )["items"].append(entry)

    # Any remaining DB rows with keys outside the catalog (legacy/unknown).
    for key in sorted(rows):
        row = rows[key]
        display = ("********" if row.value else "") if row.is_secret else coerce_out("string", row.value)
        grouped.setdefault(
            "general",
            {"title": "General", "order": 99, "category": "general", "items": []},
        )["items"].append(
            {
                "key": key,
                "value": display,
                "value_type": "string",
                "is_secret": row.is_secret,
                "label": key,
                "description": "",
            }
        )

    return {"categories": [g for g in grouped.values() if g["items"]]}


@router.put("")
async def update_settings(
    body: UpdateRequest,
    admin: Admin = Depends(get_current_admin),
    session: AsyncSession = Depends(get_session),
) -> dict[str, Any]:
    unknown = [k for k in body.values if k not in DEFAULTS_BY_KEY]
    if unknown:
        raise HTTPException(
            status.HTTP_400_BAD_REQUEST, f"Unknown setting keys: {', '.join(unknown)}"
        )
    svc = SettingsService(session)
    await svc.update_many(body.values)
    return {"ok": True, "updated": [k for k in body.values]}

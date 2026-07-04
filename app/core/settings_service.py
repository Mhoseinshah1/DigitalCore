"""Read/write access to business settings stored in the database.

The settings table stores only (key, value, is_secret); display/type metadata
(category, value_type, label, description) lives in the code catalog at
app/core/defaults.py and is looked up by key. Secret-flagged values are stored
encrypted at rest (see app/core/crypto.py).
"""
from __future__ import annotations

from typing import Any

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.core import crypto
from app.core.defaults import DEFAULTS_BY_KEY, SettingDef
from app.models.setting import Setting

_TRUE = {"1", "true", "yes", "on"}


def coerce_out(value_type: str, raw: str) -> Any:
    """Convert a stored string into a typed Python value for the API/bot."""
    if value_type == "bool":
        return str(raw).strip().lower() in _TRUE
    if value_type == "int":
        try:
            return int(raw)
        except (TypeError, ValueError):
            return 0
    return raw or ""


def coerce_in(value_type: str, value: Any) -> str:
    """Convert an incoming typed value into its stored string form."""
    if value_type == "bool":
        if isinstance(value, bool):
            return "true" if value else "false"
        return "true" if str(value).strip().lower() in _TRUE else "false"
    if value_type == "int":
        try:
            return str(int(value))
        except (TypeError, ValueError):
            return "0"
    return "" if value is None else str(value)


def meta_for(key: str) -> SettingDef | None:
    """Catalog metadata for a settings key (None for unknown keys)."""
    return DEFAULTS_BY_KEY.get(key)


def value_type_for(key: str) -> str:
    meta = meta_for(key)
    return meta.value_type if meta else "string"


class SettingsService:
    """Thin helper around the settings table."""

    def __init__(self, session: AsyncSession):
        self.session = session

    async def get_raw(self, key: str) -> Setting | None:
        result = await self.session.execute(select(Setting).where(Setting.key == key))
        return result.scalar_one_or_none()

    async def get(self, key: str, default: Any = None) -> Any:
        row = await self.get_raw(key)
        if row is None:
            return default
        stored = crypto.decrypt(row.value) if row.is_secret else row.value
        return coerce_out(value_type_for(key), stored)

    async def all_rows(self) -> list[Setting]:
        result = await self.session.execute(select(Setting))
        return list(result.scalars().all())

    async def as_public_dict(self, *, reveal_secrets: bool = False) -> dict[str, Any]:
        """Typed values for every setting; secrets masked unless revealed."""
        out: dict[str, Any] = {}
        for row in await self.all_rows():
            if row.is_secret and not reveal_secrets:
                out[row.key] = "" if not row.value else "********"
                continue
            stored = crypto.decrypt(row.value) if row.is_secret else row.value
            out[row.key] = coerce_out(value_type_for(row.key), stored)
        return out

    async def set(self, key: str, value: Any) -> Setting:
        meta = meta_for(key)
        row = await self.get_raw(key)
        if row is None:
            row = Setting(
                key=key,
                value="",
                is_secret=meta.is_secret if meta else False,
            )
            self.session.add(row)

        # A masked secret means "unchanged" — never overwrite with the mask.
        if row.is_secret and isinstance(value, str) and value == "********":
            return row

        stored = coerce_in(meta.value_type if meta else "string", value)
        row.value = crypto.encrypt(stored) if row.is_secret else stored
        return row

    async def update_many(self, values: dict[str, Any]) -> None:
        for key, value in values.items():
            if key in DEFAULTS_BY_KEY:
                await self.set(key, value)
        await self.session.commit()

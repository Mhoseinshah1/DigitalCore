"""Read/write access to business settings stored in the database.

The settings table stores only (key, value, is_secret); display/type metadata
(category, value_type, label, description) lives in the code catalog at
app/core/defaults.py and is looked up by key.

Guarantees provided by this service:
- typed reads (get_str / get_bool / get_int) that decrypt secrets;
- set() validates the value against the catalog value_type (raising ValueError
  on bad input), encrypts secret values at rest via app/core/crypto.py, and
  writes an audit_logs row for every actual change — with secret values
  redacted ("***") so plaintext secrets never reach the audit trail or logs.
"""
from __future__ import annotations

from decimal import Decimal, InvalidOperation
from typing import Any

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.core import crypto
from app.core.defaults import DEFAULTS_BY_KEY, SettingDef
from app.models.setting import Setting

_TRUE = {"1", "true", "yes", "on"}
_FALSE = {"0", "false", "no", "off", ""}

SECRET_REDACTED = "***"
SECRET_MASK = "********"


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


def validate_value(value_type: str, value: Any) -> None:
    """Raise ValueError when `value` cannot be stored as `value_type`."""
    if value_type == "bool":
        if isinstance(value, bool):
            return
        if str(value).strip().lower() in (_TRUE | _FALSE):
            return
        raise ValueError(f"expected a boolean (true/false), got {value!r}")
    if value_type == "int":
        if isinstance(value, bool):
            raise ValueError(f"expected an integer, got a boolean {value!r}")
        try:
            int(str(value).strip())
        except (TypeError, ValueError):
            raise ValueError(f"expected an integer, got {value!r}") from None


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

    async def get_str(self, key: str, default: str = "") -> str:
        value = await self.get(key, default)
        return default if value is None else str(value)

    async def get_bool(self, key: str, default: bool = False) -> bool:
        value = await self.get(key, default)
        if isinstance(value, bool):
            return value
        return str(value).strip().lower() in _TRUE

    async def get_int(self, key: str, default: int = 0) -> int:
        value = await self.get(key, default)
        try:
            return int(value)
        except (TypeError, ValueError):
            return default

    async def get_decimal(self, key: str, default: Decimal | int | str = 0) -> Decimal:
        value = await self.get(key, None)
        if value is None or value == "":
            return Decimal(str(default))
        try:
            return Decimal(str(value))
        except (InvalidOperation, TypeError, ValueError):
            return Decimal(str(default))

    async def all_rows(self) -> list[Setting]:
        result = await self.session.execute(select(Setting))
        return list(result.scalars().all())

    async def as_public_dict(self, *, reveal_secrets: bool = False) -> dict[str, Any]:
        """Typed values for every setting; secrets masked unless revealed."""
        out: dict[str, Any] = {}
        for row in await self.all_rows():
            if row.is_secret and not reveal_secrets:
                out[row.key] = "" if not row.value else SECRET_MASK
                continue
            stored = crypto.decrypt(row.value) if row.is_secret else row.value
            out[row.key] = coerce_out(value_type_for(row.key), stored)
        return out

    async def set(
        self,
        key: str,
        value: Any,
        *,
        actor_type: str = "system",
        actor_id: int | None = None,
        audit: bool = True,
    ) -> Setting:
        """Validate, store (encrypting secrets), and audit-log a setting change.

        Raises ValueError when the value does not match the catalog value_type.
        """
        meta = meta_for(key)
        value_type = meta.value_type if meta else "string"
        is_secret = meta.is_secret if meta else False

        validate_value(value_type, value)

        row = await self.get_raw(key)
        if row is None:
            row = Setting(key=key, value="", is_secret=is_secret)
            self.session.add(row)

        # A masked secret means "unchanged" — never overwrite with the mask.
        if row.is_secret and isinstance(value, str) and value == SECRET_MASK:
            return row

        old_stored = crypto.decrypt(row.value) if row.is_secret else row.value
        new_stored = coerce_in(value_type, value)

        if old_stored == new_stored:
            # No change: nothing to write, nothing to audit.
            await self.session.commit()
            return row

        row.value = crypto.encrypt(new_stored) if row.is_secret else new_stored

        if audit:
            # Import here to avoid a service <-> core import cycle at module load.
            from app.services import audit_service

            redact = row.is_secret
            await audit_service.log(
                self.session,
                actor_type=actor_type,
                actor_id=actor_id,
                action="setting.changed",
                target_type="setting",
                target_id=key,
                old=SECRET_REDACTED if redact else old_stored,
                new=SECRET_REDACTED if redact else new_stored,
            )
        else:
            await self.session.commit()
        return row

    async def update_many(
        self,
        values: dict[str, Any],
        *,
        actor_type: str = "system",
        actor_id: int | None = None,
    ) -> None:
        """Set every known key in `values`.

        All values are validated up front so a single bad value rejects the
        whole batch (ValueError) without partially applying it.
        """
        known = {k: v for k, v in values.items() if k in DEFAULTS_BY_KEY}
        for key, value in known.items():
            validate_value(value_type_for(key), value)
        for key, value in known.items():
            await self.set(key, value, actor_type=actor_type, actor_id=actor_id)
        await self.session.commit()

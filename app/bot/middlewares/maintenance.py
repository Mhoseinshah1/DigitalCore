"""Maintenance mode: when the maintenance_mode setting is on, admins still get
through; normal users receive the maintenance notice. /ping is always exempt so
health checks work even during maintenance.

The flag is read live from the settings table on each event, so flipping it in
the panel takes effect immediately. A flag getter can be injected for tests.
"""
from __future__ import annotations

import logging
from collections.abc import Awaitable, Callable
from contextlib import suppress
from typing import Any

from aiogram import BaseMiddleware
from aiogram.types import TelegramObject

from app.core.settings_service import SettingsService
from app.database import SessionLocal
from app.i18n import DEFAULT_LANG, t

log = logging.getLogger("bot.maintenance")


async def _read_flag_from_db() -> bool:
    async with SessionLocal() as session:
        return bool(await SettingsService(session).get("maintenance_mode", False))


async def _read_maintenance_text() -> str:
    async with SessionLocal() as session:
        return await SettingsService(session).get_str("maintenance_text", "")


class MaintenanceMiddleware(BaseMiddleware):
    def __init__(
        self,
        flag_getter: Callable[[], Awaitable[bool]] | None = None,
        text_getter: Callable[[], Awaitable[str]] | None = None,
    ):
        self._flag_getter = flag_getter or _read_flag_from_db
        self._text_getter = text_getter or _read_maintenance_text

    async def __call__(
        self,
        handler: Callable[[TelegramObject, dict[str, Any]], Awaitable[Any]],
        event: TelegramObject,
        data: dict[str, Any],
    ) -> Any:
        # /ping is a health check — always let it through.
        text = (getattr(event, "text", "") or "").strip()
        if text.split()[0:1] == ["/ping"]:
            return await handler(event, data)

        try:
            maintenance_on = await self._flag_getter()
        except Exception as exc:  # noqa: BLE001 - a broken flag must not kill the bot
            log.warning("Could not read maintenance_mode: %s", exc)
            maintenance_on = False

        # Any admin (owner/admin/support/…) bypasses maintenance.
        if not maintenance_on or data.get("is_admin"):
            return await handler(event, data)

        translate = data.get("_") or (lambda key, **p: t(key, DEFAULT_LANG, **p))
        try:
            custom = await self._text_getter()
        except Exception:  # noqa: BLE001
            custom = ""
        message = custom or translate("maintenance.active")
        answer = getattr(event, "answer", None)
        if callable(answer):
            with suppress(Exception):
                await answer(message)
        return None

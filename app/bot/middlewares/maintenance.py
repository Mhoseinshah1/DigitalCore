"""Maintenance mode: when the maintenance_mode setting is on, only the owner
gets through; everyone else receives a short notice.

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

from app.core.permissions import Role
from app.core.settings_service import SettingsService
from app.database import SessionLocal

log = logging.getLogger("bot.maintenance")

MAINTENANCE_MESSAGE = "🛠 The bot is under maintenance. Please try again later."


async def _read_flag_from_db() -> bool:
    async with SessionLocal() as session:
        return bool(await SettingsService(session).get("maintenance_mode", False))


class MaintenanceMiddleware(BaseMiddleware):
    def __init__(self, flag_getter: Callable[[], Awaitable[bool]] | None = None):
        self._flag_getter = flag_getter or _read_flag_from_db

    async def __call__(
        self,
        handler: Callable[[TelegramObject, dict[str, Any]], Awaitable[Any]],
        event: TelegramObject,
        data: dict[str, Any],
    ) -> Any:
        try:
            maintenance_on = await self._flag_getter()
        except Exception as exc:  # noqa: BLE001 - a broken flag must not kill the bot
            log.warning("Could not read maintenance_mode: %s", exc)
            maintenance_on = False

        if not maintenance_on or data.get("role") == Role.OWNER:
            return await handler(event, data)

        answer = getattr(event, "answer", None)
        if callable(answer):
            with suppress(Exception):
                await answer(MAINTENANCE_MESSAGE)
        return None

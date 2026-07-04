"""Force-join placeholder.

Reads the force_join_channel setting and stashes it in handler data. When the
key is unset (the default) nothing happens. Actual membership enforcement is a
later phase — this middleware only reserves the slot in the chain.
"""
from __future__ import annotations

import logging
from collections.abc import Awaitable, Callable
from typing import Any

from aiogram import BaseMiddleware
from aiogram.types import TelegramObject

from app.core.settings_service import SettingsService
from app.database import SessionLocal

log = logging.getLogger("bot.forcejoin")


class ForceJoinMiddleware(BaseMiddleware):
    async def __call__(
        self,
        handler: Callable[[TelegramObject, dict[str, Any]], Awaitable[Any]],
        event: TelegramObject,
        data: dict[str, Any],
    ) -> Any:
        channel = ""
        try:
            async with SessionLocal() as session:
                channel = str(
                    await SettingsService(session).get("force_join_channel", "") or ""
                ).strip()
        except Exception as exc:  # noqa: BLE001 - never block handling on a read failure
            log.warning("Could not read force_join_channel: %s", exc)
        data["force_join_channel"] = channel
        # Enforcement (membership check + join prompt) arrives in a later phase.
        return await handler(event, data)

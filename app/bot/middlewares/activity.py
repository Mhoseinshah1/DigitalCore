"""Refresh users.last_activity_at on every incoming event."""
from __future__ import annotations

import logging
from collections.abc import Awaitable, Callable
from typing import Any

from aiogram import BaseMiddleware
from aiogram.types import TelegramObject

from app.database import SessionLocal
from app.services import user_service

log = logging.getLogger("bot.activity")


class ActivityMiddleware(BaseMiddleware):
    async def __call__(
        self,
        handler: Callable[[TelegramObject, dict[str, Any]], Awaitable[Any]],
        event: TelegramObject,
        data: dict[str, Any],
    ) -> Any:
        tg_user = data.get("event_from_user")
        if tg_user is not None:
            try:
                async with SessionLocal() as session:
                    await user_service.touch_activity(session, tg_user.id)
            except Exception as exc:  # noqa: BLE001 - tracking must never break handling
                log.warning("Could not update last_activity_at: %s", exc)
        return await handler(event, data)

"""Attach is_admin + role to handler data for every event."""
from __future__ import annotations

import logging
from collections.abc import Awaitable, Callable
from typing import Any

from aiogram import BaseMiddleware
from aiogram.types import TelegramObject

from app.core.permissions import Role
from app.database import SessionLocal
from app.services import admin_service

log = logging.getLogger("bot.admin")


class AdminMiddleware(BaseMiddleware):
    async def __call__(
        self,
        handler: Callable[[TelegramObject, dict[str, Any]], Awaitable[Any]],
        event: TelegramObject,
        data: dict[str, Any],
    ) -> Any:
        role: Role | None = None
        tg_user = data.get("event_from_user")
        if tg_user is not None:
            try:
                async with SessionLocal() as session:
                    role = await admin_service.get_role(session, tg_user.id)
            except Exception as exc:  # noqa: BLE001 - fail closed (treated as regular user)
                log.warning("Role lookup failed: %s", exc)
        data["role"] = role
        data["is_admin"] = role is not None
        return await handler(event, data)

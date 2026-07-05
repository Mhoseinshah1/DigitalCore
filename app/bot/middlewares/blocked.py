"""Blocked-user gate: a blocked (non-admin) user gets the blocked message and
no further handling.

Runs after the admin + language middlewares so `is_admin` and the per-user
translator `_` are available. The blocked message comes from the
`blocked_user_text` setting, falling back to a bundled default. Reads live from
the DB so unblocking in the panel takes effect immediately. Fails open (treats
lookups errors as "not blocked") so a transient DB issue never locks everyone
out.
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
from app.services import user_service

log = logging.getLogger("bot.blocked")


class BlockedMiddleware(BaseMiddleware):
    async def __call__(
        self,
        handler: Callable[[TelegramObject, dict[str, Any]], Awaitable[Any]],
        event: TelegramObject,
        data: dict[str, Any],
    ) -> Any:
        # Admins are never blocked.
        if data.get("is_admin"):
            return await handler(event, data)

        tg_user = data.get("event_from_user")
        if tg_user is None:
            return await handler(event, data)

        blocked_text = ""
        try:
            async with SessionLocal() as session:
                user = await user_service.get_by_telegram_id(session, tg_user.id)
                if user is not None and user.is_blocked:
                    blocked_text = await SettingsService(session).get_str(
                        "blocked_user_text", ""
                    )
                else:
                    return await handler(event, data)
        except Exception as exc:  # noqa: BLE001 - a broken lookup must not lock users out
            log.warning("Blocked-user lookup failed: %s", exc)
            return await handler(event, data)

        translate = data.get("_") or (lambda key, **p: t(key, DEFAULT_LANG, **p))
        message = blocked_text or translate("blocked.active")
        answer = getattr(event, "answer", None)
        if callable(answer):
            with suppress(Exception):
                await answer(message)
        return None

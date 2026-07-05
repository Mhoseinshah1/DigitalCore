"""Restricted-user gate: a restricted (non-admin) user may still open the bot and
read rules/support, but cannot buy, create orders, or submit receipts.

Runs after the blocked gate. It only short-circuits *purchase actions* — a Buy
callback (``ubuy:``) or a photo/document (a receipt) — and lets everything else
through. `is_restricted` is also attached to `data` for handlers that want it.
Fails open on lookup errors.
"""
from __future__ import annotations

import logging
from collections.abc import Awaitable, Callable
from contextlib import suppress
from typing import Any

from aiogram import BaseMiddleware
from aiogram.types import CallbackQuery, TelegramObject

from app.core.settings_service import SettingsService
from app.database import SessionLocal
from app.i18n import DEFAULT_LANG, t
from app.services import user_service

log = logging.getLogger("bot.restricted")


def _is_purchase_action(event: TelegramObject) -> bool:
    # Duck-typed so it also works in unit tests: a callback has string `.data`
    # (and no photo/document); a message has `.photo`/`.document` and no `.data`.
    data = getattr(event, "data", None)
    if isinstance(data, str) and data.startswith("ubuy:"):
        return True
    return bool(getattr(event, "photo", None) or getattr(event, "document", None))


class RestrictedMiddleware(BaseMiddleware):
    async def __call__(
        self,
        handler: Callable[[TelegramObject, dict[str, Any]], Awaitable[Any]],
        event: TelegramObject,
        data: dict[str, Any],
    ) -> Any:
        if data.get("is_admin"):
            data["is_restricted"] = False
            return await handler(event, data)

        tg_user = data.get("event_from_user")
        if tg_user is None:
            return await handler(event, data)

        restricted = False
        text = ""
        try:
            async with SessionLocal() as session:
                user = await user_service.get_by_telegram_id(session, tg_user.id)
                restricted = bool(user and user.is_restricted)
                if restricted:
                    text = await SettingsService(session).get_str("restricted_user_text", "")
        except Exception as exc:  # noqa: BLE001 - never lock users out on a bad lookup
            log.warning("Restricted-user lookup failed: %s", exc)
            return await handler(event, data)

        data["is_restricted"] = restricted
        if not restricted or not _is_purchase_action(event):
            return await handler(event, data)

        translate = data.get("_") or (lambda key, **p: t(key, DEFAULT_LANG, **p))
        message = text or translate("restricted.active")
        if isinstance(event, CallbackQuery):
            with suppress(Exception):
                await event.answer(message, show_alert=True)
        else:
            answer = getattr(event, "answer", None)
            if callable(answer):
                with suppress(Exception):
                    await answer(message)
        return None

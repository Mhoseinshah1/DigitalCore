"""Resolve the user's language and inject `lang` + a `_` translator callable."""
from __future__ import annotations

import logging
from collections.abc import Awaitable, Callable
from typing import Any

from aiogram import BaseMiddleware
from aiogram.types import TelegramObject

from app.database import SessionLocal
from app.i18n import DEFAULT_LANG, normalize_lang, t
from app.services import user_service

log = logging.getLogger("bot.language")


class LanguageMiddleware(BaseMiddleware):
    async def __call__(
        self,
        handler: Callable[[TelegramObject, dict[str, Any]], Awaitable[Any]],
        event: TelegramObject,
        data: dict[str, Any],
    ) -> Any:
        lang = DEFAULT_LANG
        tg_user = data.get("event_from_user")
        if tg_user is not None:
            try:
                async with SessionLocal() as session:
                    user = await user_service.get_by_telegram_id(session, tg_user.id)
                    if user is not None:
                        lang = normalize_lang(user.language)
                    else:
                        # No user row yet (e.g. before /start): honour the admin default.
                        from app.core.settings_service import SettingsService
                        lang = normalize_lang(
                            await SettingsService(session).get_str(
                                "bot_default_language", DEFAULT_LANG))
            except Exception as exc:  # noqa: BLE001 - fall back to the default language
                log.warning("Language lookup failed: %s", exc)

        data["lang"] = lang
        data["_"] = lambda key, **params: t(key, lang, **params)
        return await handler(event, data)

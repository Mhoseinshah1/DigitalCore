"""Maintenance middleware (Phase 2): admins bypass, /ping exempt, custom text."""
from __future__ import annotations

from typing import Any

from app.bot.middlewares.maintenance import MaintenanceMiddleware
from app.i18n import DEFAULT_LANG, t

MAINTENANCE_MESSAGE = t("maintenance.active", DEFAULT_LANG)


def _flag(value: bool):
    async def getter() -> bool:
        return value

    return getter


def _text(value: str):
    async def getter() -> str:
        return value

    return getter


class FakeEvent:
    def __init__(self, text: str = "") -> None:
        self.text = text
        self.answers: list[str] = []

    async def answer(self, text: str, **kwargs: Any) -> None:
        self.answers.append(text)


def _handler(calls: list):
    async def handler(event: Any, data: dict[str, Any]) -> str:
        calls.append(event)
        return "handled"

    return handler


async def test_blocks_non_admin_when_on() -> None:
    mw = MaintenanceMiddleware(flag_getter=_flag(True), text_getter=_text(""))
    calls: list = []
    event = FakeEvent()
    result = await mw(_handler(calls), event, {"is_admin": False})
    assert result is None
    assert calls == []
    assert event.answers == [MAINTENANCE_MESSAGE]


async def test_custom_maintenance_text_used() -> None:
    mw = MaintenanceMiddleware(flag_getter=_flag(True), text_getter=_text("گشتیم نبود"))
    event = FakeEvent()
    await mw(_handler([]), event, {"is_admin": False})
    assert event.answers == ["گشتیم نبود"]


async def test_admin_passes_when_on() -> None:
    mw = MaintenanceMiddleware(flag_getter=_flag(True), text_getter=_text(""))
    calls: list = []
    event = FakeEvent()
    result = await mw(_handler(calls), event, {"is_admin": True})
    assert result == "handled"
    assert len(calls) == 1
    assert event.answers == []


async def test_ping_is_exempt_even_when_on() -> None:
    mw = MaintenanceMiddleware(flag_getter=_flag(True), text_getter=_text(""))
    calls: list = []
    event = FakeEvent(text="/ping")
    result = await mw(_handler(calls), event, {"is_admin": False})
    assert result == "handled"
    assert len(calls) == 1
    assert event.answers == []


async def test_everyone_passes_when_off() -> None:
    mw = MaintenanceMiddleware(flag_getter=_flag(False), text_getter=_text(""))
    calls: list = []
    event = FakeEvent()
    result = await mw(_handler(calls), event, {"is_admin": False})
    assert result == "handled"
    assert len(calls) == 1

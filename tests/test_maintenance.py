"""Maintenance middleware: blocks non-owners, lets the owner through."""
from __future__ import annotations

from typing import Any

from app.bot.middlewares.maintenance import MaintenanceMiddleware
from app.core.permissions import Role
from app.i18n import DEFAULT_LANG, t

MAINTENANCE_MESSAGE = t("maintenance.active", DEFAULT_LANG)


def _flag(value: bool):
    async def getter() -> bool:
        return value

    return getter


class FakeEvent:
    def __init__(self) -> None:
        self.answers: list[str] = []

    async def answer(self, text: str, **kwargs: Any) -> None:
        self.answers.append(text)


def _handler(calls: list) :
    async def handler(event: Any, data: dict[str, Any]) -> str:
        calls.append(event)
        return "handled"

    return handler


async def test_blocks_non_owner_when_on() -> None:
    mw = MaintenanceMiddleware(flag_getter=_flag(True))
    calls: list = []
    event = FakeEvent()
    result = await mw(_handler(calls), event, {"role": None})
    assert result is None
    assert calls == []
    assert event.answers == [MAINTENANCE_MESSAGE]


async def test_blocks_non_owner_admin_roles_too() -> None:
    mw = MaintenanceMiddleware(flag_getter=_flag(True))
    calls: list = []
    event = FakeEvent()
    await mw(_handler(calls), event, {"role": Role.ADMIN})
    assert calls == []


async def test_owner_passes_when_on() -> None:
    mw = MaintenanceMiddleware(flag_getter=_flag(True))
    calls: list = []
    event = FakeEvent()
    result = await mw(_handler(calls), event, {"role": Role.OWNER})
    assert result == "handled"
    assert len(calls) == 1
    assert event.answers == []


async def test_everyone_passes_when_off() -> None:
    mw = MaintenanceMiddleware(flag_getter=_flag(False))
    calls: list = []
    event = FakeEvent()
    result = await mw(_handler(calls), event, {"role": None})
    assert result == "handled"
    assert len(calls) == 1

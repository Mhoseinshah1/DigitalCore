"""Bot wiring guards: the dispatcher must register /start and /ping.

'Bot does not respond to /start' was a symptom of the bot container crashing on
startup (missing httpx). These tests lock in that the dispatcher builds and that
the user.start router still exposes /start and /ping, so a future refactor that
drops the router or the handlers fails loudly.

create_dispatcher() attaches module-level router singletons, so it can only run
once per process; a module-scoped fixture builds it exactly once.
"""
from __future__ import annotations

import pytest
from aiogram import Router

from app.bot.loader import create_dispatcher


@pytest.fixture(scope="module")
def dispatcher():
    return create_dispatcher()


def _walk_routers(router) -> list[Router]:
    routers = [router]
    for sub in router.sub_routers:
        routers.extend(_walk_routers(sub))
    return routers


def _registered_commands(dp) -> set[str]:
    commands: set[str] = set()
    for router in _walk_routers(dp):
        for handler in router.message.handlers:
            for flt in handler.filters or []:
                cb = getattr(flt, "callback", flt)
                for cmd in getattr(cb, "commands", ()) or ():
                    commands.add(str(cmd))
    return commands


def test_dispatcher_builds_and_includes_user_start_router(dispatcher) -> None:
    names = {r.name for r in _walk_routers(dispatcher)}
    assert "user.start" in names


def test_start_and_ping_are_registered(dispatcher) -> None:
    commands = _registered_commands(dispatcher)
    assert "start" in commands, "/start handler is not registered"
    assert "ping" in commands, "/ping handler is not registered"


def test_start_and_ping_handlers_exist_by_name(dispatcher) -> None:
    start_router = next(r for r in _walk_routers(dispatcher) if r.name == "user.start")
    handler_names = {h.callback.__name__ for h in start_router.message.handlers}
    assert {"on_start", "on_ping"} <= handler_names

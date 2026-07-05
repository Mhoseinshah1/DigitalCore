"""RestrictedMiddleware: purchase actions are gated for restricted users only."""
from __future__ import annotations

from typing import Any

import pytest_asyncio
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker, create_async_engine
from sqlalchemy.pool import StaticPool

import app.bot.middlewares.restricted as restricted_mod
from app.bot.middlewares.restricted import RestrictedMiddleware
from app.i18n import t
from app.models import Base, User
from app.services import user_service

FA = lambda k, **p: t(k, "fa", **p)  # noqa: E731


class FU:
    def __init__(self, uid): self.id = uid


class FMsg:
    def __init__(self, fu=None, photo=None, document=None, text=""):
        self.from_user = fu; self.photo = photo; self.document = document
        self.text = text; self.answers: list[str] = []

    async def answer(self, t, **k): self.answers.append(t)


class FCb:
    def __init__(self, data, fu):
        self.data = data; self.from_user = fu; self.alerts: list[str] = []

    async def answer(self, t="", **k):
        if t:
            self.alerts.append(t)


@pytest_asyncio.fixture
async def db(monkeypatch):
    engine = create_async_engine("sqlite+aiosqlite://", poolclass=StaticPool,
                                 connect_args={"check_same_thread": False})
    async with engine.begin() as conn:
        await conn.run_sync(Base.metadata.create_all)
    maker = async_sessionmaker(engine, class_=AsyncSession, expire_on_commit=False)
    monkeypatch.setattr(restricted_mod, "SessionLocal", maker)
    try:
        yield maker
    finally:
        await engine.dispose()


async def _mk_user(maker, tg, *, restricted=False):
    async with maker() as s:
        u = User(telegram_id=tg, first_name="U")
        u.is_restricted = restricted
        s.add(u)
        await s.commit()


async def _run(event, *, is_admin=False):
    mw = RestrictedMiddleware()
    calls: list = []

    async def handler(ev, data):
        calls.append(ev)
        return "handled"

    data = {"is_admin": is_admin, "event_from_user": event.from_user, "_": FA}
    result = await mw(handler, event, data)
    return result, calls, data


async def test_restricted_user_cannot_buy(db) -> None:
    await _mk_user(db, 10, restricted=True)
    cb = FCb("ubuy:1", FU(10))
    result, calls, _data = await _run(cb)
    assert result is None and calls == []  # short-circuited
    assert cb.alerts  # got the restricted message


async def test_restricted_user_cannot_submit_receipt(db) -> None:
    await _mk_user(db, 11, restricted=True)
    msg = FMsg(FU(11), photo=[object()])
    result, calls, _data = await _run(msg)
    assert result is None and calls == []
    assert msg.answers


async def test_restricted_user_can_do_other_things(db) -> None:
    await _mk_user(db, 12, restricted=True)
    msg = FMsg(FU(12), text="/start")
    result, calls, data = await _run(msg)
    assert result == "handled" and calls  # passed through
    assert data["is_restricted"] is True


async def test_normal_user_passes(db) -> None:
    await _mk_user(db, 13, restricted=False)
    cb = FCb("ubuy:1", FU(13))
    result, calls, _data = await _run(cb)
    assert result == "handled" and calls


async def test_admin_bypasses_restriction(db) -> None:
    await _mk_user(db, 14, restricted=True)
    cb = FCb("ubuy:1", FU(14))
    result, calls, data = await _run(cb, is_admin=True)
    assert result == "handled" and calls
    assert data["is_restricted"] is False

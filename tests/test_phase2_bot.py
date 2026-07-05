"""Phase 2 bot: /start registration, /ping, /products, blocked-user middleware.

Bot handlers use the module-level SessionLocal, so the fixture points that at a
fresh in-memory SQLite engine (create_all) in every handler module under test.
"""
from __future__ import annotations

from typing import Any

import pytest_asyncio
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker, create_async_engine
from sqlalchemy.pool import StaticPool

import app.bot.handlers.user.products as products_mod
import app.bot.handlers.user.start as start_mod
import app.bot.middlewares.blocked as blocked_mod
from app.bot.handlers.user.products import on_products
from app.bot.handlers.user.start import on_ping, on_start
from app.bot.middlewares.blocked import BlockedMiddleware
from app.i18n import t
from app.models import Base, Product, User
from app.services import user_service

FA = lambda key, **p: t(key, "fa", **p)  # noqa: E731 - test translator


class FakeUser:
    def __init__(self, uid, username=None, first_name="F", last_name=None, language_code=None):
        self.id = uid
        self.username = username
        self.first_name = first_name
        self.last_name = last_name
        self.language_code = language_code


class FakeMessage:
    def __init__(self, from_user=None, text=""):
        self.from_user = from_user
        self.text = text
        self.answers: list[str] = []

    async def answer(self, text: str, **kwargs: Any) -> None:
        self.answers.append(text)


@pytest_asyncio.fixture
async def bot_db(monkeypatch):
    engine = create_async_engine(
        "sqlite+aiosqlite://", poolclass=StaticPool,
        connect_args={"check_same_thread": False},
    )
    async with engine.begin() as conn:
        await conn.run_sync(Base.metadata.create_all)
    maker = async_sessionmaker(engine, class_=AsyncSession, expire_on_commit=False)
    for mod in (start_mod, products_mod, blocked_mod):
        monkeypatch.setattr(mod, "SessionLocal", maker)
    try:
        yield maker
    finally:
        await engine.dispose()


async def test_start_registers_new_user_and_captures_language(bot_db) -> None:
    msg = FakeMessage(FakeUser(70001, username="neo", first_name="Neo", language_code="en"))
    await on_start(msg, FA, lang="fa", is_admin=False)
    # New user gets the language picker.
    assert msg.answers and msg.answers[0] == FA("lang.pick")
    async with bot_db() as s:
        user = await user_service.get_by_telegram_id(s, 70001)
    assert user is not None and user.language_code == "en"


async def test_start_existing_user_shows_menu(bot_db) -> None:
    async with bot_db() as s:
        await user_service.create_or_update_from_telegram(s, telegram_id=70002, first_name="Old")
    msg = FakeMessage(FakeUser(70002, first_name="Old"))
    await on_start(msg, FA, lang="fa", is_admin=False)
    # Existing user gets the greeting (start_text is empty -> i18n greeting).
    assert msg.answers and msg.answers[0] == FA("greeting")


async def test_ping_responds_pong(bot_db) -> None:
    msg = FakeMessage(FakeUser(70003), text="/ping")
    await on_ping(msg, FA)
    assert msg.answers == [FA("ping")]


class FakeState:
    """Minimal FSMContext stand-in: just enough for handlers that clear state."""

    def __init__(self) -> None:
        self.cleared = False
        self._data: dict = {}

    async def clear(self) -> None:
        self.cleared = True
        self._data = {}

    async def set_state(self, *_a, **_k) -> None:
        pass

    async def update_data(self, **kw) -> None:
        self._data.update(kw)

    async def get_data(self) -> dict:
        return dict(self._data)


async def test_products_placeholder_when_empty(bot_db) -> None:
    msg = FakeMessage(FakeUser(70004))
    await on_products(msg, FA, FakeState(), lang="fa")
    assert msg.answers == [FA("products.user.empty")]


async def test_products_lists_active_visible(bot_db) -> None:
    async with bot_db() as s:
        s.add(Product(type="license", title="Gold Plan", price=50000,
                      is_active=True, is_hidden=False))
        s.add(Product(type="license", title="Hidden Plan", price=1,
                      is_active=True, is_hidden=True))
        await s.commit()
    msg = FakeMessage(FakeUser(70005))
    await on_products(msg, FA, FakeState(), lang="fa")
    body = "\n".join(msg.answers)
    assert "Gold Plan" in body
    assert "Hidden Plan" not in body


async def test_blocked_middleware_blocks_non_admin(bot_db) -> None:
    async with bot_db() as s:
        user, _ = await user_service.create_or_update_from_telegram(s, telegram_id=70006)
        await user_service.admin_set_blocked(s, user.id, True)
        await s.commit()

    mw = BlockedMiddleware()
    event = FakeMessage(FakeUser(70006))
    calls: list = []

    async def handler(ev, data):
        calls.append(ev)
        return "handled"

    result = await mw(handler, event, {"is_admin": False, "event_from_user": event.from_user, "_": FA})
    assert result is None
    assert calls == []
    assert event.answers == [FA("blocked.active")]


def test_v2ray_detail_shows_specs_and_safe_server_only() -> None:
    """The detail view renders duration/traffic/ip_limit and a friendly server
    name, but must never expose panel base_url/username/inbound id."""
    from app.bot.handlers.user.products import build_detail_lines

    product = Product(
        type="v2ray", title="Germany 30d", price=90000,
        duration_days=30, traffic_gb=50, ip_limit=2,
        xui_server_id=1, xui_inbound_id=1,
    )
    body = "\n".join(build_detail_lines(product, "Germany-1", "en"))
    assert "30 days" in body
    assert "50 GB" in body
    assert "IP limit: 2" in body
    assert "Germany-1" in body  # safe label
    # None of the sensitive credentials/ids leak (they are never passed in).
    for leak in ("http://", "root", "base_url", "username", "inbound_id"):
        assert leak not in body


def test_license_detail_omits_v2ray_specs() -> None:
    from app.bot.handlers.user.products import build_detail_lines

    product = Product(type="license", title="Win Key", price=150000)
    body = "\n".join(build_detail_lines(product, None, "en"))
    assert "Win Key" in body
    assert "days" not in body and "GB" not in body and "IP limit" not in body


async def test_blocked_middleware_passes_admin_and_normal_user(bot_db) -> None:
    async with bot_db() as s:
        user, _ = await user_service.create_or_update_from_telegram(s, telegram_id=70007)
        await user_service.admin_set_blocked(s, user.id, True)
        await user_service.create_or_update_from_telegram(s, telegram_id=70008)
        await s.commit()

    mw = BlockedMiddleware()

    async def handler(ev, data):
        return "handled"

    # Blocked user but is_admin -> passes.
    admin_ev = FakeMessage(FakeUser(70007))
    assert await mw(handler, admin_ev, {"is_admin": True, "event_from_user": admin_ev.from_user, "_": FA}) == "handled"

    # Non-blocked normal user -> passes.
    normal_ev = FakeMessage(FakeUser(70008))
    assert await mw(handler, normal_ev, {"is_admin": False, "event_from_user": normal_ev.from_user, "_": FA}) == "handled"

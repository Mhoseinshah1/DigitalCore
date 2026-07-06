"""Bot purchase flow: buy -> instructions -> receipt (mocked download) -> orders."""
from __future__ import annotations

from typing import Any

import pytest_asyncio
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker, create_async_engine
from sqlalchemy.pool import StaticPool

import app.bot.handlers.user.orders as orders_mod
import app.bot.notifications as notify_mod
from app.bot.handlers.user.orders import on_buy, on_orders, on_receipt_in_state
from app.i18n import t
from app.models import Base, Product, Setting, User
from app.services import order_service, payment_service

FA = lambda key, **p: t(key, "fa", **p)  # noqa: E731


class FakeUser:
    def __init__(self, uid, username="buyer", first_name="B", last_name=None):
        self.id = uid
        self.username = username
        self.first_name = first_name
        self.last_name = last_name


class FakeMessage:
    def __init__(self, from_user=None, photo=None, document=None, text=""):
        self.from_user = from_user
        self.photo = photo
        self.document = document
        self.text = text
        self.answers: list[str] = []

    async def answer(self, text: str, **kwargs: Any) -> None:
        self.answers.append(text)


class FakeCallback:
    def __init__(self, data, from_user, message):
        self.data = data
        self.from_user = from_user
        self.message = message
        self.alerts: list[str] = []

    async def answer(self, text: str = "", **kwargs: Any) -> None:
        if text:
            self.alerts.append(text)


class FakePhoto:
    def __init__(self, file_id, file_size):
        self.file_id = file_id
        self.file_size = file_size


class FakeState:
    def __init__(self):
        self._data: dict = {}
        self.state = None

    async def clear(self):
        self._data = {}
        self.state = None

    async def set_state(self, state):
        self.state = state

    async def update_data(self, **kw):
        self._data.update(kw)

    async def get_data(self):
        return dict(self._data)


class FakeBot:
    def __init__(self):
        self.sent: list[tuple] = []

    async def send_message(self, chat_id, text, **kw):
        self.sent.append(("message", chat_id, text))

    async def send_photo(self, chat_id, file_id, **kw):
        self.sent.append(("photo", chat_id, file_id))

    async def send_document(self, chat_id, file_id, **kw):
        self.sent.append(("document", chat_id, file_id))


@pytest_asyncio.fixture
async def bot_db(monkeypatch, tmp_path):
    engine = create_async_engine(
        "sqlite+aiosqlite://", poolclass=StaticPool,
        connect_args={"check_same_thread": False},
    )
    async with engine.begin() as conn:
        await conn.run_sync(Base.metadata.create_all)
    maker = async_sessionmaker(engine, class_=AsyncSession, expire_on_commit=False)
    monkeypatch.setattr(orders_mod, "SessionLocal", maker)
    monkeypatch.setattr(notify_mod, "SessionLocal", maker)
    monkeypatch.setattr(payment_service, "RECEIPTS_ROOT", tmp_path)
    # Deterministic file "download".
    async def fake_download(bot, file_id):
        return b"\x89PNG\r\n\x1a\n" + b"receipt-bytes"
    monkeypatch.setattr(orders_mod, "_download_telegram_file", fake_download)
    try:
        yield maker
    finally:
        await engine.dispose()


async def _seed_product(maker, *, card="6037-0000-0000-0000") -> int:
    async with maker() as s:
        s.add(Setting(key="card_number", value=card))
        # These tests isolate the card-to-card flow; wallet payment is exercised
        # in tests/test_wallet_bot.py, so turn it off here to bypass the picker.
        s.add(Setting(key="wallet_payment_enabled", value="false"))
        p = Product(type="license", title="Gold Key", price=120000,
                    is_active=True, is_hidden=False)
        s.add(p)
        await s.commit()
        return p.id


async def test_buy_creates_order_and_shows_instructions(bot_db) -> None:
    pid = await _seed_product(bot_db)
    msg = FakeMessage(FakeUser(4001))
    cb = FakeCallback(f"ubuy:{pid}", FakeUser(4001), msg)
    state = FakeState()
    await on_buy(cb, FakeBot(), FA, state, lang="fa")

    body = "\n".join(msg.answers)
    assert "DC-" in body  # order number shown
    assert "6037-0000-0000-0000" in body  # card number shown
    async with bot_db() as s:
        u = (await __import__("app.services.user_service", fromlist=["get_by_telegram_id"])
             .get_by_telegram_id(s, 4001))
        orders = await order_service.list_user_orders(s, u.id)
    assert len(orders) == 1 and orders[0].status == "pending_payment"


async def test_buy_blocks_when_card_not_configured(bot_db) -> None:
    pid = await _seed_product(bot_db, card="")  # empty card number
    msg = FakeMessage(FakeUser(4002))
    cb = FakeCallback(f"ubuy:{pid}", FakeUser(4002), msg)
    await on_buy(cb, FakeBot(), FA, FakeState(), lang="fa")
    assert msg.answers == [FA("purchase.not_configured")]


async def test_receipt_submission_flow(bot_db) -> None:
    pid = await _seed_product(bot_db)
    buyer = FakeUser(4003)
    # Buy first.
    buy_msg = FakeMessage(buyer)
    state = FakeState()
    await on_buy(FakeCallback(f"ubuy:{pid}", buyer, buy_msg), FakeBot(), FA, state, lang="fa")

    # Now send a photo receipt (state carries order_id).
    photo_msg = FakeMessage(buyer, photo=[FakePhoto("bigfile", 2048)])
    await on_receipt_in_state(photo_msg, FakeBot(), FA, state, lang="fa")

    assert FA("purchase.receipt_saved") in photo_msg.answers
    async with bot_db() as s:
        us = await __import__("app.services.user_service", fromlist=["get_by_telegram_id"]).get_by_telegram_id(s, 4003)
        orders = await order_service.list_user_orders(s, us.id)
        payment = await payment_service.get_payment_by_order(s, orders[0].id)
    assert orders[0].status == "waiting_admin"
    assert payment.status == "receipt_submitted"


async def test_receipt_without_pending_order_is_helpful(bot_db) -> None:
    await _seed_product(bot_db)
    # A user who never bought anything sends a photo out of the blue.
    async with bot_db() as s:
        s.add(User(telegram_id=4004, first_name="Nobody"))
        await s.commit()
    photo_msg = FakeMessage(FakeUser(4004), photo=[FakePhoto("f", 1000)])
    # Stateless path: order_id=None -> latest pending -> none.
    await orders_mod.on_receipt_stateless(photo_msg, FakeBot(), FA, FakeState(), lang="fa")
    assert FA("purchase.no_pending_order") in photo_msg.answers


async def test_orders_list_shows_user_orders(bot_db) -> None:
    pid = await _seed_product(bot_db)
    buyer = FakeUser(4005)
    await on_buy(FakeCallback(f"ubuy:{pid}", buyer, FakeMessage(buyer)), FakeBot(), FA, FakeState(), lang="fa")

    msg = FakeMessage(buyer)
    await on_orders(msg, FA, FakeState(), lang="fa")
    body = "\n".join(msg.answers)
    assert "DC-" in body
    assert FA("order.status.pending_payment") in body


async def test_orders_list_empty(bot_db) -> None:
    msg = FakeMessage(FakeUser(4006))
    await on_orders(msg, FA, FakeState(), lang="fa")
    assert msg.answers == [FA("orders.user.empty")]

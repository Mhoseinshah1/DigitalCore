"""Phase 7 bot: /wallet balance, history, and wallet payment for an order."""
from __future__ import annotations

from typing import Any

import pytest
import pytest_asyncio
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker, create_async_engine
from sqlalchemy.pool import StaticPool

import app.bot.handlers.user.orders as orders_mod
import app.bot.handlers.user.wallet as wallet_mod
from app.bot.handlers.user.wallet import on_wallet, _send_history
from app.bot.handlers.user.orders import _pay_with_wallet
from app.i18n import t
from app.models import Base, Product, User, WalletTransaction
from app.services import license_service

FA = lambda k, **p: t(k, "fa", **p)  # noqa: E731


class FU:
    def __init__(self, uid, username="u"):
        self.id = uid; self.username = username
        self.first_name = "B"; self.last_name = None


class FM:
    def __init__(self, fu=None):
        self.from_user = fu; self.answers: list[str] = []; self.markups: list = []

    async def answer(self, text, **k):
        self.answers.append(text); self.markups.append(k.get("reply_markup"))


class FS:
    def __init__(self): self._d: dict[str, Any] = {}
    async def clear(self): self._d = {}
    async def set_state(self, *a, **k): pass
    async def update_data(self, **k): self._d.update(k)
    async def get_data(self): return dict(self._d)


@pytest.fixture(autouse=True)
def _stub_delivery(monkeypatch):
    async def _ok(bot, order, product, lic, lang="fa"):
        return True
    monkeypatch.setattr(license_service, "_deliver_to_user", _ok)


@pytest_asyncio.fixture
async def db(monkeypatch):
    engine = create_async_engine("sqlite+aiosqlite://", poolclass=StaticPool,
                                 connect_args={"check_same_thread": False})
    async with engine.begin() as conn:
        await conn.run_sync(Base.metadata.create_all)
    maker = async_sessionmaker(engine, class_=AsyncSession, expire_on_commit=False)
    monkeypatch.setattr(wallet_mod, "SessionLocal", maker)
    monkeypatch.setattr(orders_mod, "SessionLocal", maker)
    try:
        yield maker
    finally:
        await engine.dispose()


async def _user(maker, tg=10, balance=0):
    async with maker() as s:
        u = User(telegram_id=tg, first_name="B", language="fa", wallet_balance=balance)
        s.add(u)
        await s.commit()
        return u.id


async def _license_product(maker, price=50_000, stock=True):
    async with maker() as s:
        p = Product(type="license", title="Netflix", price=price, is_active=True, is_hidden=False)
        s.add(p)
        await s.flush()
        pid = p.id
        if stock:
            await license_service.add_license(s, pid, "a@x.com", "pw", admin_id=9)
        await s.commit()
        return pid


async def test_wallet_shows_balance(db) -> None:
    await _user(db, tg=10, balance=42_000)
    msg = FM(FU(10))
    await on_wallet(msg, FA, FS(), lang="fa")
    body = "\n".join(msg.answers)
    assert "42,000" in body


async def test_wallet_history(db) -> None:
    uid = await _user(db, tg=11, balance=0)
    async with db() as s:
        s.add(WalletTransaction(user_id=uid, amount=10_000, balance_before=0,
                                balance_after=10_000, type="deposit", reason="topup"))
        await s.commit()
    msg = FM(FU(11))
    await _send_history(msg, FA, "fa")
    body = "\n".join(msg.answers)
    assert "10,000" in body and FA("wallet.tx.deposit") in body


async def test_wallet_payment_success(db) -> None:
    uid = await _user(db, tg=12, balance=100_000)
    pid = await _license_product(db, price=50_000)
    reply = FM(FU(12))
    await _pay_with_wallet(reply, FU(12), pid, FA, None)
    body = "\n".join(reply.answers)
    assert "50,000" in body  # new balance shown
    async with db() as s:
        from app.services import wallet_service
        assert await wallet_service.get_balance(s, uid) == 50_000


async def test_wallet_payment_insufficient(db) -> None:
    uid = await _user(db, tg=13, balance=1_000)
    pid = await _license_product(db, price=50_000)
    reply = FM(FU(13))
    await _pay_with_wallet(reply, FU(13), pid, FA, None)
    body = "\n".join(reply.answers)
    assert FA("purchase.wallet.insufficient", balance="1,000", amount="50,000") in body
    async with db() as s:
        from app.services import wallet_service
        assert await wallet_service.get_balance(s, uid) == 1_000  # unchanged

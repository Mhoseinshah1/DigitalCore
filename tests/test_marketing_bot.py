"""Phase 10 bot: coupon entry in the buy flow, /referral stats, and /start ref_."""
from __future__ import annotations

from typing import Any

import pytest_asyncio
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker, create_async_engine
from sqlalchemy.pool import StaticPool

import app.bot.handlers.user.orders as orders_mod
import app.bot.handlers.user.referral as ref_mod
import app.bot.handlers.user.start as start_mod
from app.i18n import t
from app.models import Base, Product, User
from app.services import coupon_service, referral_service, user_service

FA = lambda k, **p: t(k, "fa", **p)  # noqa: E731


class FU:
    def __init__(self, uid): self.id = uid; self.username = None
    first_name = "U"; last_name = None; language_code = "fa"


class FMsg:
    def __init__(self, fu, text=None):
        self.from_user = fu; self.text = text; self.answers: list = []

    async def answer(self, txt, **k): self.answers.append(txt)


class FState:
    def __init__(self): self._s = None; self._d: dict[str, Any] = {}
    async def clear(self): self._s = None; self._d = {}
    async def set_state(self, s): self._s = s
    async def get_state(self): return self._s
    async def update_data(self, **kw): self._d.update(kw)
    async def get_data(self): return dict(self._d)


class FBotUser:
    username = "DigitalCoreBot"


class FBot:
    async def get_me(self): return FBotUser()


class FCommand:
    def __init__(self, args): self.args = args


@pytest_asyncio.fixture
async def db(monkeypatch):
    engine = create_async_engine("sqlite+aiosqlite://", poolclass=StaticPool,
                                 connect_args={"check_same_thread": False})
    async with engine.begin() as conn:
        await conn.run_sync(Base.metadata.create_all)
    maker = async_sessionmaker(engine, class_=AsyncSession, expire_on_commit=False)
    for mod in (orders_mod, ref_mod, start_mod):
        monkeypatch.setattr(mod, "SessionLocal", maker)
    try:
        yield maker
    finally:
        await engine.dispose()


async def test_coupon_code_valid_applies(db) -> None:
    async with db() as s:
        u = User(telegram_id=10, first_name="U")
        s.add(u)
        await s.flush()
        p = Product(type="license", title="Win", price=100_000, is_active=True)
        s.add(p)
        await s.flush()
        await coupon_service.create_coupon(s, code="TEN", discount_type="percent",
                                           discount_value=10)
        await s.commit()
        pid = p.id
    state = FState()
    await state.update_data(buy_product_id=pid)
    msg = FMsg(FU(10), text="ten")
    await orders_mod.on_coupon_code(msg, FBot(), FA, state)
    body = "\n".join(msg.answers)
    assert "TEN" in body and "90,000" in body  # applied message shows the final amount
    assert (await state.get_data()).get("buy_coupon") == "TEN"


async def test_coupon_code_invalid_errors(db) -> None:
    async with db() as s:
        u = User(telegram_id=11, first_name="U")
        s.add(u)
        await s.flush()
        p = Product(type="license", title="Win", price=100_000, is_active=True)
        s.add(p)
        await s.commit()
        pid = p.id
    state = FState()
    await state.update_data(buy_product_id=pid)
    msg = FMsg(FU(11), text="NOPE")
    await orders_mod.on_coupon_code(msg, FBot(), FA, state)
    assert any(a == FA("coupon.err.coupon_not_found") for a in msg.answers)


async def test_referral_shows_link_and_stats(db) -> None:
    async with db() as s:
        u = User(telegram_id=20, first_name="U")
        s.add(u)
        await s.commit()
    msg = FMsg(FU(20))
    await ref_mod.on_referral(msg, FBot(), FA, FState())
    body = "\n".join(msg.answers)
    assert "t.me/DigitalCoreBot?start=ref_" in body
    assert "کاربران دعوت‌شده" in body or "Invited" in body


async def test_start_ref_registers_referrer(db) -> None:
    async with db() as s:
        referrer = User(telegram_id=1, first_name="Ref")
        s.add(referrer)
        await s.commit()
        code = await referral_service.get_or_create_referral_code(s, referrer.id)
        await s.commit()
        ref_id = referrer.id
    # New user starts with the referral deep link.
    msg = FMsg(FU(2))
    await start_mod.on_start(msg, FA, command=FCommand(f"ref_{code}"), is_admin=False)
    async with db() as s:
        new = await user_service.get_by_telegram_id(s, 2)
        assert new is not None and new.referrer_id == ref_id


async def test_start_self_ref_ignored(db) -> None:
    async with db() as s:
        u = User(telegram_id=5, first_name="U")
        s.add(u)
        await s.commit()
        code = await referral_service.get_or_create_referral_code(s, u.id)
        await s.commit()
    msg = FMsg(FU(5))
    await start_mod.on_start(msg, FA, command=FCommand(f"ref_{code}"), is_admin=False)
    async with db() as s:
        u = await user_service.get_by_telegram_id(s, 5)
        assert u.referrer_id is None

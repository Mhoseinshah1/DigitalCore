"""Phase 5 bot: delivery message format + /my_licenses ownership."""
from __future__ import annotations

from typing import Any

import pytest_asyncio
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker, create_async_engine
from sqlalchemy.pool import StaticPool

import app.bot.handlers.user.orders as orders_mod
from app.bot.handlers.user.orders import on_license_detail, on_my_licenses
from app.i18n import t
from app.models import Base, LicenseItem, Product, User
from app.services import license_service

FA = lambda k, **p: t(k, "fa", **p)  # noqa: E731


class FU:
    def __init__(self, uid): self.id = uid


class FM:
    def __init__(self, fu=None):
        self.from_user = fu; self.answers: list[str] = []

    async def answer(self, t, **k): self.answers.append(t)


class FCB:
    def __init__(self, data, fu, msg):
        self.data = data; self.from_user = fu; self.message = msg; self.alerts: list[str] = []

    async def answer(self, t="", **k):
        if t:
            self.alerts.append(t)


class FS:
    def __init__(self): self._d: dict[str, Any] = {}
    async def clear(self): self._d = {}


@pytest_asyncio.fixture
async def db(monkeypatch):
    engine = create_async_engine("sqlite+aiosqlite://", poolclass=StaticPool,
                                 connect_args={"check_same_thread": False})
    async with engine.begin() as conn:
        await conn.run_sync(Base.metadata.create_all)
    maker = async_sessionmaker(engine, class_=AsyncSession, expire_on_commit=False)
    monkeypatch.setattr(orders_mod, "SessionLocal", maker)
    try:
        yield maker
    finally:
        await engine.dispose()


def test_delivery_message_format() -> None:
    class _O:
        order_number = "DC-1"
    class _P:
        title = "Netflix"
    lic = LicenseItem(product_id=1, email="a@x.com", password="pw123", note="hi", status="sold")
    msg = license_service.build_delivery_message(_O(), _P(), lic, "fa")
    assert "DC-1" in msg and "Netflix" in msg
    assert "a@x.com" in msg and "pw123" in msg and "hi" in msg


async def _sold_license(maker, tg, email="a@x.com"):
    async with maker() as s:
        u = User(telegram_id=tg, first_name="B", language="fa")
        s.add(u)
        p = Product(type="license", title="Netflix", price=1000, is_active=True, is_hidden=False)
        s.add(p)
        await s.flush()
        lic = LicenseItem(product_id=p.id, email=email, password="pw", status="sold",
                          sold_to_user_id=u.id)
        s.add(lic)
        await s.commit()
        return u.id, lic.id


async def test_my_licenses_shows_user_licenses(db) -> None:
    uid, lid = await _sold_license(db, tg=10)
    msg = FM(FU(10))
    await on_my_licenses(msg, FA, FS(), lang="fa")
    body = "\n".join(msg.answers)
    assert "Netflix" in body


async def test_my_licenses_empty(db) -> None:
    async with db() as s:
        s.add(User(telegram_id=11, first_name="Nobody"))
        await s.commit()
    msg = FM(FU(11))
    await on_my_licenses(msg, FA, FS(), lang="fa")
    assert msg.answers == [FA("licenses.user.empty")]


async def test_license_detail_only_own(db) -> None:
    uid, lid = await _sold_license(db, tg=12, email="owner@x.com")
    # A different user must not see this license.
    async with db() as s:
        s.add(User(telegram_id=13, first_name="Other"))
        await s.commit()
    cb = FCB(f"ulic:{lid}", FU(13), FM(FU(13)))
    await on_license_detail(cb, FA, lang="fa")
    assert cb.alerts and cb.alerts[0] == FA("licenses.user.not_found")

    # The owner sees the credentials.
    owner_msg = FM(FU(12))
    cb2 = FCB(f"ulic:{lid}", FU(12), owner_msg)
    await on_license_detail(cb2, FA, lang="fa")
    assert any("owner@x.com" in a for a in owner_msg.answers)

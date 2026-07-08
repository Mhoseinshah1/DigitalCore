"""Bot «سفارش‌های من» pagination/detail and «لایسنس‌های من» list/detail + ownership."""
from __future__ import annotations

from typing import Any

import pytest_asyncio
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker, create_async_engine
from sqlalchemy.pool import StaticPool

import app.bot.handlers.user.orders as orders_mod
from app.i18n import t
from app.models import Base, LicenseItem, Order, Product, User

FA = lambda key, **p: t(key, "fa", **p)  # noqa: E731


class FU:
    def __init__(self, uid):
        self.id = uid
        self.username = "u"
        self.first_name = "F"
        self.last_name = None


class FM:
    def __init__(self, from_user=None, text=""):
        self.from_user = from_user
        self.text = text
        self.answers: list[str] = []
        self.edits: list[str] = []
        self.markups: list[Any] = []

    async def answer(self, text: str, **kw: Any) -> None:
        self.answers.append(text)
        self.markups.append(kw.get("reply_markup"))

    async def edit_text(self, text: str, **kw: Any) -> None:
        self.edits.append(text)
        self.markups.append(kw.get("reply_markup"))

    async def delete(self) -> None:
        pass


class FC:
    def __init__(self, data, from_user, message):
        self.data = data
        self.from_user = from_user
        self.message = message
        self.alerts: list[str] = []

    async def answer(self, text: str = "", **kw: Any) -> None:
        if text:
            self.alerts.append(text)


class FState:
    def __init__(self):
        self.state = None
        self._d: dict = {}

    async def clear(self):
        self.state = None
        self._d = {}

    async def set_state(self, s):
        self.state = s

    async def update_data(self, **kw):
        self._d.update(kw)

    async def get_data(self):
        return dict(self._d)


def _btns(markup) -> list[str]:
    return [] if markup is None else [b.text for row in markup.inline_keyboard for b in row]


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


async def _seed_user(maker, tid) -> int:
    async with maker() as s:
        u = User(telegram_id=tid, first_name="B")
        s.add(u)
        await s.commit()
        return u.id


async def _seed_orders(maker, user_id, n, *, product_title="Apple ID USA"):
    async with maker() as s:
        p = Product(type="license", title=product_title, price=1000, is_active=True)
        s.add(p)
        await s.commit()
        for i in range(n):
            s.add(Order(order_number=f"DC-2026-{user_id:03d}{i:03d}", user_id=user_id,
                        product_id=p.id, amount=1000, final_amount=1000,
                        status="pending_payment", payment_method="card"))
        await s.commit()
        return p.id


async def _seed_licenses(maker, user_id, n):
    async with maker() as s:
        p = Product(type="license", title="Apple ID USA", price=1000, is_active=True)
        s.add(p)
        await s.commit()
        for i in range(n):
            o = Order(order_number=f"DC-LIC-{user_id:03d}{i:03d}", user_id=user_id,
                      product_id=p.id, amount=1000, final_amount=1000,
                      status="delivered", payment_method="card")
            s.add(o)
            await s.commit()
            s.add(LicenseItem(product_id=p.id, email=f"user{i}@x.com", password=f"pw{i}",
                              status="sold", sold_to_user_id=user_id, order_id=o.id))
        await s.commit()


# --------------------------------------------------------------------------
# Orders
# --------------------------------------------------------------------------
async def test_orders_empty_message(db) -> None:
    await _seed_user(db, 3001)
    msg = FM(FU(3001))
    await orders_mod.on_orders(msg, FA, FState(), lang="fa")
    assert msg.answers == [FA("orders.user.empty")]


async def test_orders_paginated_and_detail(db) -> None:
    uid = await _seed_user(db, 3002)
    await _seed_orders(db, uid, 7)  # 7 orders -> 2 pages of 5
    msg = FM(FU(3002))
    await orders_mod.on_orders(msg, FA, FState(), lang="fa")
    body = msg.answers[0]
    assert FA("orders.page_of", page=1, pages=2) in body
    labels = _btns(msg.markups[0])
    assert FA("orders.btn.detail", index=1) in labels
    assert FA("btn.next") in labels and FA("btn.prev") not in labels  # page 1

    # Go to page 2 (edit in place).
    cb = FC(f"{orders_mod.CB_ORDER_PAGE}1", FU(3002), FM(FU(3002)))
    await orders_mod.on_orders_page(cb, FA, lang="fa")
    assert FA("orders.page_of", page=2, pages=2) in cb.message.edits[0]
    assert FA("btn.prev") in _btns(cb.message.markups[0])

    # Open a detail (order id 1 belongs to this user).
    cb2 = FC(f"{orders_mod.CB_ORDER_DETAIL}1:0", FU(3002), FM(FU(3002)))
    await orders_mod.on_order_detail(cb2, FA, lang="fa")
    detail = cb2.message.edits[0]
    assert FA("orders.detail.title") in detail
    assert "DC-2026-" in detail
    assert FA("order.method.card") in detail


async def test_order_detail_rejects_other_user(db) -> None:
    uid = await _seed_user(db, 3003)
    await _seed_orders(db, uid, 1)
    await _seed_user(db, 3004)  # attacker
    cb = FC(f"{orders_mod.CB_ORDER_DETAIL}1:0", FU(3004), FM(FU(3004)))
    await orders_mod.on_order_detail(cb, FA, lang="fa")
    assert cb.alerts == [FA("orders.detail.not_found")]
    assert not cb.message.edits


# --------------------------------------------------------------------------
# Licenses
# --------------------------------------------------------------------------
async def test_licenses_empty_message(db) -> None:
    await _seed_user(db, 4001)
    msg = FM(FU(4001))
    await orders_mod.on_my_licenses(msg, FA, FState(), lang="fa")
    assert len(msg.answers) == 1 and FA("licenses.user.empty") in msg.answers[0]


async def test_licenses_list_numbered_with_orders_and_detail(db) -> None:
    uid = await _seed_user(db, 4002)
    await _seed_licenses(db, uid, 2)
    msg = FM(FU(4002))
    await orders_mod.on_my_licenses(msg, FA, FState(), lang="fa")
    body = msg.answers[0]
    assert FA("licenses.user.pick") in body
    assert "DC-LIC-" in body                 # order number shown
    assert "Apple ID USA" in body            # product title shown
    labels = _btns(msg.markups[0])
    assert any("DC-LIC-" in lbl for lbl in labels)  # inline item buttons

    # Open a license detail — credentials shown to the owner.
    async with db() as s:
        from sqlalchemy import select
        lic_id = (await s.execute(select(LicenseItem.id).where(
            LicenseItem.sold_to_user_id == uid).limit(1))).scalar_one()
    cb = FC(f"{orders_mod.CB_LICENSE}{lic_id}:0", FU(4002), FM(FU(4002)))
    await orders_mod.on_license_detail(cb, FA, lang="fa")
    detail = cb.message.edits[0]
    assert FA("licenses.detail.title") in detail
    assert "@x.com" in detail and "pw" in detail  # email + password


async def test_license_detail_rejects_other_user(db) -> None:
    uid = await _seed_user(db, 4003)
    await _seed_licenses(db, uid, 1)
    await _seed_user(db, 4004)  # attacker
    async with db() as s:
        from sqlalchemy import select
        lic_id = (await s.execute(select(LicenseItem.id).where(
            LicenseItem.sold_to_user_id == uid).limit(1))).scalar_one()
    cb = FC(f"{orders_mod.CB_LICENSE}{lic_id}:0", FU(4004), FM(FU(4004)))
    await orders_mod.on_license_detail(cb, FA, lang="fa")
    assert cb.alerts == [FA("licenses.user.not_found")]
    assert not cb.message.edits


async def test_licenses_paginated(db) -> None:
    uid = await _seed_user(db, 4005)
    await _seed_licenses(db, uid, 7)  # 2 pages
    msg = FM(FU(4005))
    await orders_mod.on_my_licenses(msg, FA, FState(), lang="fa")
    assert FA("btn.next") in _btns(msg.markups[0])
    cb = FC(f"{orders_mod.CB_LIC_PAGE}1", FU(4005), FM(FU(4005)))
    await orders_mod.on_licenses_page(cb, FA, lang="fa")
    assert FA("btn.prev") in _btns(cb.message.markups[0])

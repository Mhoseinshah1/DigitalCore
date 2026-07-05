"""Telegram admin receipt quick actions: FSM, permissions, ownership guard."""
from __future__ import annotations

import tempfile
from pathlib import Path
from typing import Any

import pytest_asyncio
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker, create_async_engine
from sqlalchemy.pool import StaticPool

import app.bot.handlers.admin.receipt_actions as ra
from app.bot.handlers.admin.receipt_actions import (
    ReceiptActionStates,
    on_amount,
    on_reason,
    on_receipt_action,
)
from app.core.permissions import Role
from app.i18n import t
from app.models import Base, Product, User
from app.services import license_service, order_service, payment_service, user_service
from app.services.payment_service import ReceiptFile

FA = lambda k, **p: t(k, "fa", **p)  # noqa: E731
ADMIN = 999


class FU:
    def __init__(self, uid): self.id = uid; self.username = "bob"; self.first_name = "B"


class FM:
    def __init__(self, fu=None, text=""):
        self.from_user = fu; self.text = text; self.answers: list[str] = []

    async def answer(self, t, **k): self.answers.append(t)


class FCB:
    def __init__(self, data, fu, msg):
        self.data = data; self.from_user = fu; self.message = msg; self.alerts: list[str] = []

    async def answer(self, t="", **k):
        if t:
            self.alerts.append(t)


class FS:
    def __init__(self): self._d: dict[str, Any] = {}; self.st = None
    async def set_state(self, st): self.st = st
    async def update_data(self, **k): self._d.update(k)
    async def get_data(self): return dict(self._d)
    async def clear(self): self._d = {}; self.st = None


@pytest_asyncio.fixture
async def db(monkeypatch):
    engine = create_async_engine("sqlite+aiosqlite://", poolclass=StaticPool,
                                 connect_args={"check_same_thread": False})
    async with engine.begin() as conn:
        await conn.run_sync(Base.metadata.create_all)
    maker = async_sessionmaker(engine, class_=AsyncSession, expire_on_commit=False)
    monkeypatch.setattr(ra, "SessionLocal", maker)
    monkeypatch.setattr(payment_service, "RECEIPTS_ROOT", Path(tempfile.mkdtemp()))
    try:
        yield maker
    finally:
        await engine.dispose()


async def _submitted(maker) -> tuple[int, int]:
    async with maker() as s:
        u = User(telegram_id=7, username="bob", first_name="B", wallet_balance=0)
        s.add(u)
        p = Product(type="license", title="Key", price=50000, is_active=True, is_hidden=False)
        s.add(p)
        await s.flush()
        await license_service.add_keys(s, p.id, ["LIC-1"], actor_id=ADMIN)
        order = await order_service.create_order(s, u.id, p.id)
        await payment_service.create_payment_for_order(s, order)
        fi = ReceiptFile(content=b"\x89PNG\r\n\x1a\n" + b"x" * 20,
                         original_name="r.png", mime_type="image/png", file_id="f")
        await payment_service.submit_receipt(s, order.id, u.id, fi)
        await s.commit()
        return order.id, u.id


async def test_non_admin_callback_rejected(db) -> None:
    oid, _uid = await _submitted(db)
    cb = FCB(f"radm:approve:{oid}", FU(ADMIN), FM(FU(ADMIN)))
    await on_receipt_action(cb, FA, FS(), role=None, is_admin=False, lang="fa")
    assert cb.alerts and cb.alerts[0] == FA("radm.not_authorized")


async def test_admin_approve_delivers(db) -> None:
    oid, _uid = await _submitted(db)
    msg = FM(FU(ADMIN))
    cb = FCB(f"radm:approve:{oid}", FU(ADMIN), msg)
    await on_receipt_action(cb, FA, FS(), role=Role.ADMIN, is_admin=True, lang="fa")
    assert FA("radm.approved") in msg.answers
    async with db() as s:
        o = await order_service.get_order(s, oid)
    assert o.status == "delivered"


async def test_add_balance_fsm(db) -> None:
    oid, uid = await _submitted(db)
    st = FS()
    cb = FCB(f"radm:addbal:{oid}", FU(ADMIN), FM(FU(ADMIN)))
    await on_receipt_action(cb, FA, st, role=Role.ADMIN, is_admin=True, lang="fa")
    assert st.st == ReceiptActionStates.waiting_amount

    # Amount validation: bad input re-asks.
    bad = FM(FU(ADMIN), text="abc")
    await on_amount(bad, FA, st)
    assert FA("radm.invalid_amount") in bad.answers

    good = FM(FU(ADMIN), text="15000")
    await on_amount(good, FA, st)
    assert st.st == ReceiptActionStates.waiting_reason

    reason = FM(FU(ADMIN), text="promo")
    await on_reason(reason, FA, st, lang="fa")
    assert FA("radm.wallet_added") in reason.answers
    async with db() as s:
        u = await user_service.get_by_id(s, uid)
    assert u.wallet_balance == 15000


async def test_wrong_admin_cannot_complete(db) -> None:
    oid, uid = await _submitted(db)
    st = FS()
    cb = FCB(f"radm:addbal:{oid}", FU(ADMIN), FM(FU(ADMIN)))
    await on_receipt_action(cb, FA, st, role=Role.ADMIN, is_admin=True, lang="fa")
    # A different admin sending a number must be ignored (no state change).
    other = FM(FU(12345), text="9999")
    await on_amount(other, FA, st)
    assert other.answers == []
    assert st.st == ReceiptActionStates.waiting_amount  # still waiting for the owner


async def test_reject_fsm_stores_reason(db) -> None:
    oid, _uid = await _submitted(db)
    st = FS()
    cb = FCB(f"radm:reject:{oid}", FU(ADMIN), FM(FU(ADMIN)))
    await on_receipt_action(cb, FA, st, role=Role.ADMIN, is_admin=True, lang="fa")
    reason = FM(FU(ADMIN), text="blurry")
    await on_reason(reason, FA, st, lang="fa")
    async with db() as s:
        o = await order_service.get_order(s, oid)
    assert o.status == "rejected" and o.reject_reason == "blurry"


async def test_restrict_fsm(db) -> None:
    oid, uid = await _submitted(db)
    st = FS()
    cb = FCB(f"radm:restrict:{oid}", FU(ADMIN), FM(FU(ADMIN)))
    await on_receipt_action(cb, FA, st, role=Role.ADMIN, is_admin=True, lang="fa")
    reason = FM(FU(ADMIN), text="watch")
    await on_reason(reason, FA, st, lang="fa")
    async with db() as s:
        u = await user_service.get_by_id(s, uid)
    assert u.is_restricted and u.restriction_reason == "watch"


async def test_block_requires_confirmation(db) -> None:
    oid, uid = await _submitted(db)
    msg = FM(FU(ADMIN))
    cb = FCB(f"radm:block:{oid}", FU(ADMIN), msg)
    await on_receipt_action(cb, FA, FS(), role=Role.ADMIN, is_admin=True, lang="fa")
    assert FA("radm.block_confirm") in msg.answers
    async with db() as s:
        u = await user_service.get_by_id(s, uid)
    assert not u.is_blocked  # not blocked until confirmed

    msg2 = FM(FU(ADMIN))
    cb2 = FCB(f"radm:blockok:{oid}", FU(ADMIN), msg2)
    await on_receipt_action(cb2, FA, FS(), role=Role.ADMIN, is_admin=True, lang="fa")
    async with db() as s:
        u = await user_service.get_by_id(s, uid)
    assert u.is_blocked


async def test_accountant_cannot_block(db) -> None:
    oid, _uid = await _submitted(db)
    cb = FCB(f"radm:block:{oid}", FU(ADMIN), FM(FU(ADMIN)))
    await on_receipt_action(cb, FA, FS(), role=Role.ACCOUNTANT, is_admin=True, lang="fa")
    assert cb.alerts and cb.alerts[0] == FA("radm.not_authorized")

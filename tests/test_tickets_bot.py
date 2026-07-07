"""Phase 9 bot flows: user /support (create/list/open/reply), /tutorials, and the
Telegram admin ticket actions (list/reply). No real Telegram — direct handler calls."""
from __future__ import annotations

from typing import Any

import pytest_asyncio
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker, create_async_engine
from sqlalchemy.pool import StaticPool

import app.bot.handlers.admin.tickets as atk
import app.bot.handlers.user.tickets as utk
import app.bot.handlers.user.tutorials as utut
from app.core.permissions import Role
from app.i18n import t
from app.models import Base, User
from app.services import ticket_service, tutorial_service

FA = lambda k, **p: t(k, "fa", **p)  # noqa: E731


class FU:
    def __init__(self, uid): self.id = uid; self.username = None
    first_name = "U"; last_name = None


class FMsg:
    def __init__(self, fu, text=None, caption=None):
        self.from_user = fu; self.text = text; self.caption = caption
        self.photo = None; self.document = None; self.answers: list[str] = []

    async def answer(self, txt, **k): self.answers.append(txt)


class FCB:
    def __init__(self, data, fu, msg):
        self.data = data; self.from_user = fu; self.message = msg; self.alerts: list[str] = []

    async def answer(self, txt="", **k):
        if txt:
            self.alerts.append(txt)


class FState:
    def __init__(self): self._s = None; self._d: dict[str, Any] = {}
    async def clear(self): self._s = None; self._d = {}
    async def set_state(self, s): self._s = s
    async def get_state(self): return self._s
    async def update_data(self, **kw): self._d.update(kw)
    async def get_data(self): return dict(self._d)


class FCommand:
    def __init__(self, args): self.args = args


@pytest_asyncio.fixture
async def db(monkeypatch):
    engine = create_async_engine("sqlite+aiosqlite://", poolclass=StaticPool,
                                 connect_args={"check_same_thread": False})
    async with engine.begin() as conn:
        await conn.run_sync(Base.metadata.create_all)
    maker = async_sessionmaker(engine, class_=AsyncSession, expire_on_commit=False)
    for mod in (utk, utut, atk):
        monkeypatch.setattr(mod, "SessionLocal", maker)
    try:
        yield maker
    finally:
        await engine.dispose()


# --- user support flow ------------------------------------------------------
async def test_user_support_create_list_open(db) -> None:
    fu = FU(10)
    state = FState()
    # /support menu
    m = FMsg(fu)
    await utk.on_support(m, FA, state)
    assert any("پشتیبانی" in a or a for a in m.answers)
    # start new ticket → subject → message
    await utk._start_new_ticket(m, FA, state)
    await utk.on_subject(FMsg(fu, text="Cannot connect"), FA, state)
    create_msg = FMsg(fu, text="my vpn is down")
    await utk.on_message_text(create_msg, FA, state)
    assert any("TK-" in a for a in create_msg.answers)
    # list shows the ticket
    list_msg = FMsg(fu)
    await utk.on_my_tickets(list_msg, FA, state)
    assert any("Cannot connect" in a for a in list_msg.answers)
    # open it (find id)
    async with db() as s:
        user = await s.scalar(__import__("sqlalchemy").select(User).where(User.telegram_id == 10))
        tickets = await ticket_service.list_user_tickets(s, user.id)
        tid = tickets[0].id
    detail = FMsg(fu)
    cb = FCB(f"{utk.CB_OPEN}{tid}", fu, detail)
    await utk.on_open(cb, FA)
    assert any("my vpn is down" in a for a in detail.answers)


async def test_user_cannot_open_others_ticket(db) -> None:
    async with db() as s:
        owner = User(telegram_id=1, first_name="O")
        s.add(owner)
        await s.flush()
        tk = await ticket_service.create_ticket(s, owner.id, "secret", "private")
        await s.commit()
        tid = tk.id
        s.add(User(telegram_id=2, first_name="X"))
        await s.commit()
    cb = FCB(f"{utk.CB_OPEN}{tid}", FU(2), FMsg(FU(2)))
    await utk.on_open(cb, FA)
    assert cb.alerts and cb.alerts[0] == FA("tickets.not_found")


async def test_user_reply_flow(db) -> None:
    async with db() as s:
        u = User(telegram_id=5, first_name="U")
        s.add(u)
        await s.flush()
        tk = await ticket_service.create_ticket(s, u.id, "s", "hi")
        await s.commit()
        tid = tk.id
    state = FState()
    fu = FU(5)
    cb = FCB(f"{utk.CB_REPLY}{tid}", fu, FMsg(fu))
    await utk.on_reply_cb(cb, FA, state)
    reply = FMsg(fu, text="any update?")
    await utk.on_reply_text(reply, FA, state)
    assert any(a == FA("tickets.reply_sent") for a in reply.answers)
    async with db() as s:
        tk2 = await ticket_service.get_ticket(s, tid)
        assert tk2.status == "pending_admin" and len(tk2.messages) == 2


# --- tutorials --------------------------------------------------------------
async def test_tutorials_home_and_read(db) -> None:
    async with db() as s:
        cat = await tutorial_service.create_category(s, "Guides")
        active = await tutorial_service.create_tutorial(
            s, title="Android", content="connect like this", category_id=cat.id,
            platform="android")
        hidden = await tutorial_service.create_tutorial(
            s, title="Draft", content="wip", is_active=False)
        await s.commit()
        active_id, hidden_id = active.id, hidden.id
    m = FMsg(FU(9))
    await utut.on_tutorials(m, FA, FState())
    assert m.answers  # home shown
    # reading an active tutorial shows its content
    read = FMsg(FU(9))
    await utut.on_read(FCB(f"{utut.CB_READ}{active_id}", FU(9), read), FA)
    assert any("connect like this" in a for a in read.answers)
    # an inactive tutorial is never exposed
    cb = FCB(f"{utut.CB_READ}{hidden_id}", FU(9), FMsg(FU(9)))
    await utut.on_read(cb, FA)
    assert cb.alerts and cb.alerts[0] == FA("tutorials.not_found")


# --- telegram admin ---------------------------------------------------------
async def test_admin_tickets_list_and_reply(db) -> None:
    async with db() as s:
        u = User(telegram_id=77, first_name="U")
        s.add(u)
        await s.flush()
        tk = await ticket_service.create_ticket(s, u.id, "need help", "broken")
        await s.commit()
        tid = tk.id
    # non-admin is refused
    m = FMsg(FU(1))
    await atk.on_admin_tickets(m, FA, FState(), role=None, is_admin=False)
    assert m.answers and m.answers[0] == FA("radm.not_authorized")
    # owner sees the open list
    m2 = FMsg(FU(1))
    await atk.on_admin_tickets(m2, FA, FState(), role=Role.OWNER, is_admin=True)
    assert any("need help" in a for a in m2.answers)
    # owner replies via the reply state (bot=None → notification is a no-op)
    state = FState()
    await state.set_state(atk.TicketAdminStates.waiting_reply)
    await state.update_data(ticket_id=tid, admin_tg=1)
    reply = FMsg(FU(1), text="we are on it")
    await atk.on_admin_reply(reply, FA, state, bot=None, is_admin=True)
    assert reply.answers and reply.answers[0] == FA("atickets.reply_sent")
    async with db() as s:
        tk2 = await ticket_service.get_ticket(s, tid)
        assert tk2.status == "pending_user"
        assert any(mm.sender_type == "admin" for mm in tk2.messages)

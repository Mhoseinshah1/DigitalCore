"""Admin bot: creating a V2Ray product now collects the XUI server + inbound
binding (the missing step that caused «v2ray products require an XUI server»)."""
from __future__ import annotations

from typing import Any

import pytest_asyncio
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker, create_async_engine
from sqlalchemy.pool import StaticPool

import app.bot.handlers.admin.products as products_mod
from app.bot.states.products import ProductAddForm
from app.core.permissions import Role
from app.i18n import t
from app.models import Base, XuiInbound, XuiServer
from app.services import product_service

FA = lambda key, **p: t(key, "fa", **p)  # noqa: E731


class FU:
    def __init__(self, uid=700):
        self.id = uid
        self.username = "adm"
        self.first_name = "A"
        self.last_name = "B"


class FM:
    def __init__(self, from_user=None, text=""):
        self.from_user = from_user
        self.text = text
        self.answers: list[str] = []
        self.markups: list[Any] = []

    async def answer(self, text: str, **kwargs: Any) -> None:
        self.answers.append(text)
        self.markups.append(kwargs.get("reply_markup"))

    async def edit_text(self, text: str, **kwargs: Any) -> None:
        self.answers.append(text)
        self.markups.append(kwargs.get("reply_markup"))


class FC:
    def __init__(self, data, from_user, message):
        self.data = data
        self.from_user = from_user
        self.message = message
        self.alerts: list[str] = []

    async def answer(self, text: str = "", **kwargs: Any) -> None:
        if text:
            self.alerts.append(text)


class FState:
    def __init__(self):
        self._data: dict = {}
        self.state = None

    async def clear(self):
        self._data = {}
        self.state = None

    async def set_state(self, state):
        self.state = state

    async def get_state(self):
        return self.state

    async def update_data(self, **kw):
        self._data.update(kw)

    async def get_data(self):
        return dict(self._data)


def _cb_datas(markup) -> list[str]:
    if markup is None:
        return []
    return [b.callback_data for row in markup.inline_keyboard for b in row]


@pytest_asyncio.fixture
async def db(monkeypatch):
    engine = create_async_engine("sqlite+aiosqlite://", poolclass=StaticPool,
                                 connect_args={"check_same_thread": False})
    async with engine.begin() as conn:
        await conn.run_sync(Base.metadata.create_all)
    maker = async_sessionmaker(engine, class_=AsyncSession, expire_on_commit=False)
    monkeypatch.setattr(products_mod, "SessionLocal", maker)
    try:
        yield maker
    finally:
        await engine.dispose()


async def _seed_server(maker, *, active=True, with_inbound=True, inbound_active=True,
                       n_inbounds=1):
    async with maker() as s:
        srv = XuiServer(name="Germany", base_url="http://x", is_active=active, status="active")
        s.add(srv)
        await s.commit()
        inb_id = None
        for i in range(n_inbounds if with_inbound else 0):
            remark = "VLESS-Reality" if n_inbounds == 1 else f"VLESS-Reality-{i}"
            inb = XuiInbound(server_id=srv.id, inbound_id=7 + i, remark=remark,
                             protocol="vless", port=443 + i, is_active=inbound_active)
            s.add(inb)
            await s.commit()
            if inb_id is None:
                inb_id = inb.id
        return srv.id, inb_id


async def _walk_to_traffic(maker, state, *, ptype="v2ray"):
    """Drive type → title → price → (duration → traffic)."""
    user = FU()
    await products_mod.on_type_chosen(FC(f"ptype:{ptype}", user, FM(user)), state, FA, role=Role.OWNER)
    await products_mod.on_title(FM(user, text="Gold VPN"), state, FA)
    await products_mod.on_price(FM(user, text="250000"), state, FA, role=Role.OWNER)
    if ptype == "v2ray":
        await products_mod.on_duration(FM(user, text="30"), state, FA)
        traffic_msg = FM(user, text="50")
        await products_mod.on_traffic(traffic_msg, state, FA, lang="fa", role=Role.OWNER)
        return traffic_msg
    return None


# ==========================================================================
# Happy path
# ==========================================================================
async def test_v2ray_asks_for_server_after_traffic(db) -> None:
    await _seed_server(db)
    state = FState()
    traffic_msg = await _walk_to_traffic(db, state)
    # After traffic the bot asks for a server (does NOT create the product yet).
    assert state.state == ProductAddForm.choosing_server
    assert traffic_msg.answers[-1] == FA("products.v2ray.pick_server")
    assert any(cb.startswith(products_mod.CB_ADD_SRV) for cb in _cb_datas(traffic_msg.markups[-1]))


async def test_v2ray_server_choice_shows_inbounds(db) -> None:
    # With MORE THAN ONE active inbound the admin gets a picker (auto-select only
    # kicks in when there is exactly one).
    srv_id, _inb = await _seed_server(db, n_inbounds=2)
    state = FState()
    await _walk_to_traffic(db, state)
    msg = FM(FU())
    cb = FC(f"{products_mod.CB_ADD_SRV}{srv_id}", FU(), msg)
    await products_mod.on_add_server_chosen(cb, state, FA, lang="fa", role=Role.OWNER)
    assert state.state == ProductAddForm.choosing_inbound
    assert msg.answers[-1] == FA("products.v2ray.pick_inbound", server="Germany")
    assert any(cb_.startswith(products_mod.CB_ADD_INB) for cb_ in _cb_datas(msg.markups[-1]))


async def test_single_inbound_is_auto_selected(db) -> None:
    # Exactly one active inbound → the admin never types/picks an id; it is bound
    # automatically and the flow jumps straight to the device-limit step.
    srv_id, inb_id = await _seed_server(db)  # one inbound
    state = FState()
    await _walk_to_traffic(db, state)
    msg = FM(FU())
    await products_mod.on_add_server_chosen(
        FC(f"{products_mod.CB_ADD_SRV}{srv_id}", FU(), msg), state, FA, lang="fa", role=Role.OWNER)
    assert state.state == ProductAddForm.entering_ip_limit
    # The confirmation naming the auto-selected inbound was shown.
    assert any("VLESS-Reality" in a for a in msg.answers)
    data = await state.get_data()
    assert data["xui_inbound_id"] == inb_id


async def test_v2ray_full_flow_creates_bound_product(db) -> None:
    srv_id, inb_id = await _seed_server(db)
    state = FState()
    await _walk_to_traffic(db, state)
    # choose server → the sole inbound is auto-selected → asks device limit
    await products_mod.on_add_server_chosen(
        FC(f"{products_mod.CB_ADD_SRV}{srv_id}", FU(), FM(FU())), state, FA,
        lang="fa", role=Role.OWNER)
    assert state.state == ProductAddForm.entering_ip_limit
    # skip device limit → asks description
    ip_msg = FM(FU())
    await products_mod.on_skip_ip(FC(products_mod.CB_ADD_SKIP_IP, FU(), ip_msg), state, FA, role=Role.OWNER)
    assert state.state == ProductAddForm.entering_description
    # skip description → creates the product
    done_msg = FM(FU())
    await products_mod.on_skip_desc(FC(products_mod.CB_ADD_SKIP_DESC, FU(), done_msg), state, FA, role=Role.OWNER)

    # Success message is the big style and names the server + inbound.
    body = done_msg.answers[-1]
    assert FA("products.created_title") in body
    assert "Germany" in body and "VLESS-Reality" in body
    # The product was persisted WITH its binding (no validation error).
    async with db() as s:
        products = await product_service.list_for_admin(s)
    assert len(products) == 1
    p = products[0]
    assert p.type == "v2ray"
    assert p.xui_server_id == srv_id
    assert p.xui_inbound_id == inb_id
    assert p.duration_days == 30 and p.traffic_gb == 50


async def test_v2ray_device_limit_is_saved(db) -> None:
    srv_id, inb_id = await _seed_server(db)
    state = FState()
    await _walk_to_traffic(db, state)
    # server choice auto-selects the sole inbound → straight to device-limit step
    await products_mod.on_add_server_chosen(
        FC(f"{products_mod.CB_ADD_SRV}{srv_id}", FU(), FM(FU())), state, FA, lang="fa", role=Role.OWNER)
    # enter a device limit of 3, then skip description
    await products_mod.on_ip_limit(FM(FU(), text="3"), state, FA, role=Role.OWNER)
    await products_mod.on_skip_desc(FC(products_mod.CB_ADD_SKIP_DESC, FU(), FM(FU())), state, FA, role=Role.OWNER)
    async with db() as s:
        p = (await product_service.list_for_admin(s))[0]
    assert p.ip_limit == 3
    assert p.xui_inbound_id == inb_id


async def test_v2ray_multi_inbound_pick_then_create(db) -> None:
    # The multi-inbound path still lets the admin pick which inbound to bind.
    srv_id, inb_id = await _seed_server(db, n_inbounds=2)
    state = FState()
    await _walk_to_traffic(db, state)
    await products_mod.on_add_server_chosen(
        FC(f"{products_mod.CB_ADD_SRV}{srv_id}", FU(), FM(FU())), state, FA, lang="fa", role=Role.OWNER)
    assert state.state == ProductAddForm.choosing_inbound
    await products_mod.on_add_inbound_chosen(
        FC(f"{products_mod.CB_ADD_INB}{inb_id}", FU(), FM(FU())), state, FA, lang="fa", role=Role.OWNER)
    assert state.state == ProductAddForm.entering_ip_limit
    await products_mod.on_skip_ip(FC(products_mod.CB_ADD_SKIP_IP, FU(), FM(FU())), state, FA, role=Role.OWNER)
    await products_mod.on_skip_desc(FC(products_mod.CB_ADD_SKIP_DESC, FU(), FM(FU())), state, FA, role=Role.OWNER)
    async with db() as s:
        p = (await product_service.list_for_admin(s))[0]
    assert p.xui_inbound_id == inb_id


# ==========================================================================
# Error states
# ==========================================================================
async def test_no_active_server_shows_clear_error(db) -> None:
    await _seed_server(db, active=False)  # server exists but inactive
    state = FState()
    traffic_msg = await _walk_to_traffic(db, state)
    assert traffic_msg.answers[-1] == FA("products.v2ray.no_server")
    assert state.state is None  # flow ended, nothing created


async def test_no_active_inbound_shows_clear_error(db) -> None:
    srv_id, _inb = await _seed_server(db, inbound_active=False)  # inbound exists but inactive
    state = FState()
    await _walk_to_traffic(db, state)
    msg = FM(FU())
    await products_mod.on_add_server_chosen(
        FC(f"{products_mod.CB_ADD_SRV}{srv_id}", FU(), msg), state, FA, lang="fa", role=Role.OWNER)
    assert msg.answers[-1] == FA("products.v2ray.no_inbound")
    # Offers sync-guidance + choose-another-server buttons.
    datas = _cb_datas(msg.markups[-1])
    assert any(d.startswith(products_mod.CB_ADD_SYNC) for d in datas)
    assert products_mod.CB_ADD_SRVLIST in datas


async def test_sync_button_pulls_inbounds_then_auto_selects(db, monkeypatch) -> None:
    # Tapping «sync inbounds» from the product flow runs a REAL sync, then the
    # freshly-synced (sole) inbound is auto-selected — no manual id typing.
    from app.services import xui_inbound_sync_service as sync_mod

    srv_id, _inb = await _seed_server(db, with_inbound=False)  # nothing synced yet
    state = FState()
    await _walk_to_traffic(db, state)
    msg = FM(FU())
    await products_mod.on_add_server_chosen(
        FC(f"{products_mod.CB_ADD_SRV}{srv_id}", FU(), msg), state, FA, lang="fa", role=Role.OWNER)
    assert msg.answers[-1] == FA("products.v2ray.no_inbound")

    async def fake_sync(session, server_id, **kw):
        session.add(XuiInbound(server_id=server_id, inbound_id=9, remark="Synced-VLESS",
                               protocol="vless", port=2087, is_active=True))
        await session.commit()
        return sync_mod.SyncResult(server_id=server_id, server_name="Germany",
                                   success=True, created_count=1, total_remote_count=1)

    monkeypatch.setattr(products_mod.xui_inbound_sync_service, "sync_server_inbounds", fake_sync)
    sync_msg = FM(FU())
    await products_mod.on_add_sync_hint(
        FC(f"{products_mod.CB_ADD_SYNC}{srv_id}", FU(), sync_msg), state, FA,
        lang="fa", role=Role.OWNER)
    assert any(a == FA("products.v2ray.sync_done", count=1) for a in sync_msg.answers)
    assert state.state == ProductAddForm.entering_ip_limit  # sole inbound auto-selected


async def test_sync_button_failure_reoffers_buttons(db, monkeypatch) -> None:
    from app.services import xui_inbound_sync_service as sync_mod

    srv_id, _inb = await _seed_server(db, with_inbound=False)
    state = FState()
    await _walk_to_traffic(db, state)

    async def fake_sync(session, server_id, **kw):
        return sync_mod.SyncResult(server_id=server_id, server_name="Germany",
                                   success=False, error_message="unreachable")

    monkeypatch.setattr(products_mod.xui_inbound_sync_service, "sync_server_inbounds", fake_sync)
    sync_msg = FM(FU())
    await products_mod.on_add_sync_hint(
        FC(f"{products_mod.CB_ADD_SYNC}{srv_id}", FU(), sync_msg), state, FA,
        lang="fa", role=Role.OWNER)
    assert sync_msg.answers[-1] == FA("products.v2ray.sync_failed", message="unreachable")
    datas = _cb_datas(sync_msg.markups[-1])
    assert any(d.startswith(products_mod.CB_ADD_SYNC) for d in datas)


# ==========================================================================
# License products never ask for a server/inbound
# ==========================================================================
async def test_license_flow_does_not_ask_for_server(db) -> None:
    await _seed_server(db)  # a server exists, but license must not use it
    state = FState()
    user = FU()
    await products_mod.on_type_chosen(FC("ptype:license", user, FM(user)), state, FA, role=Role.OWNER)
    await products_mod.on_title(FM(user, text="Windows Key"), state, FA)
    price_msg = FM(user, text="90000")
    await products_mod.on_price(price_msg, state, FA, role=Role.OWNER)
    # License create happens immediately — no server/inbound prompt, no FSM left.
    assert state.state is None
    assert price_msg.answers[-1] == FA("products.created", title="Windows Key")
    async with db() as s:
        p = (await product_service.list_for_admin(s))[0]
    assert p.type == "license"
    assert p.xui_server_id is None and p.xui_inbound_id is None

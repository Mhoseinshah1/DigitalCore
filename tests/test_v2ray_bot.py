"""Phase 6 bot: /my_services list + ownership isolation + empty state."""
from __future__ import annotations

from typing import Any

import pytest_asyncio
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker, create_async_engine
from sqlalchemy.pool import StaticPool

import app.bot.handlers.user.services as svc_mod
from app.bot.handlers.user.services import on_my_services, on_service_detail
from app.i18n import t
from app.models import Base, Order, Product, User, V2RayService, XuiInbound, XuiServer

FA = lambda k, **p: t(k, "fa", **p)  # noqa: E731


class FU:
    def __init__(self, uid): self.id = uid


class FM:
    def __init__(self, fu=None):
        self.from_user = fu; self.answers: list[str] = []

    async def answer(self, txt, **k): self.answers.append(txt)


class FCB:
    def __init__(self, data, fu, msg):
        self.data = data; self.from_user = fu; self.message = msg; self.alerts: list[str] = []

    async def answer(self, txt="", **k):
        if txt:
            self.alerts.append(txt)


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
    monkeypatch.setattr(svc_mod, "SessionLocal", maker)
    try:
        yield maker
    finally:
        await engine.dispose()


async def _service(maker, tg, *, sub="https://sub.example.com/sub/abc"):
    async with maker() as s:
        u = User(telegram_id=tg, first_name="B", language="fa")
        srv = XuiServer(name="srv", base_url="http://p:2053", panel_version="2.9.4",
                        is_active=True, status="online")
        s.add_all([u, srv]); await s.flush()
        ib = XuiInbound(server_id=srv.id, inbound_id=55, is_active=True)
        s.add(ib); await s.flush()
        p = Product(type="v2ray", title="VPN-30", price=1000, duration_days=30, traffic_gb=10,
                    ip_limit=1, is_active=True, is_hidden=False,
                    xui_server_id=srv.id, xui_inbound_id=ib.id)
        s.add(p); await s.flush()
        o = Order(order_number="DC-B-1", user_id=u.id, product_id=p.id, amount=1000,
                  final_amount=1000, status="delivered", payment_method="card_to_card")
        s.add(o); await s.flush()
        svc = V2RayService(user_id=u.id, order_id=o.id, product_id=p.id,
                           xui_server_id=srv.id, xui_inbound_id=ib.id,
                           client_email="dc-u1-odc-b-1", client_uuid="uuid-1",
                           sub_id="abc", subscription_url=sub, total_gb=10 * 1024 ** 3,
                           used_gb=0, ip_limit=1, status="active")
        s.add(svc); await s.commit()
        return u.id, svc.id


async def test_my_services_shows_services(db) -> None:
    await _service(db, tg=10)
    msg = FM(FU(10))
    await on_my_services(msg, FA, FS(), lang="fa")
    body = "\n".join(msg.answers)
    assert "VPN-30" in body


async def test_my_services_empty(db) -> None:
    async with db() as s:
        s.add(User(telegram_id=11, first_name="Nobody"))
        await s.commit()
    msg = FM(FU(11))
    await on_my_services(msg, FA, FS(), lang="fa")
    assert msg.answers == [FA("services.user.empty")]


async def test_service_detail_only_own(db) -> None:
    uid, sid = await _service(db, tg=12)
    async with db() as s:
        s.add(User(telegram_id=13, first_name="Other"))
        await s.commit()
    # A different user must not see this service.
    cb = FCB(f"usvc:{sid}", FU(13), FM(FU(13)))
    await on_service_detail(cb, FA, lang="fa")
    assert cb.alerts and cb.alerts[0] == FA("services.user.not_found")

    # The owner sees the subscription link.
    owner_msg = FM(FU(12))
    cb2 = FCB(f"usvc:{sid}", FU(12), owner_msg)
    await on_service_detail(cb2, FA, lang="fa")
    assert any("sub.example.com" in a for a in owner_msg.answers)

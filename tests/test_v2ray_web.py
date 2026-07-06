"""Phase 6 web: v2ray service pages auth/permission + actions, no secret leak."""
from __future__ import annotations

from collections.abc import AsyncIterator

import httpx
import pytest_asyncio
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker, create_async_engine
from sqlalchemy.pool import StaticPool

from app.core import crypto
from app.core.security import hash_password
from app.database import get_session
from app.models import Admin, Base, Order, Product, User, V2RayService, XuiInbound, XuiServer
from app.services import v2ray_service
from app.web.main import app

PW = "v2ray-web-1"


@pytest_asyncio.fixture
async def env(monkeypatch) -> AsyncIterator[dict]:
    engine = create_async_engine("sqlite+aiosqlite://", poolclass=StaticPool,
                                 connect_args={"check_same_thread": False})
    async with engine.begin() as conn:
        await conn.run_sync(Base.metadata.create_all)
    maker = async_sessionmaker(engine, class_=AsyncSession, expire_on_commit=False)

    async def _override():
        async with maker() as s:
            yield s

    app.dependency_overrides[get_session] = _override
    transport = httpx.ASGITransport(app=app)
    clients: list[httpx.AsyncClient] = []

    async def login(role: str) -> httpx.AsyncClient:
        async with maker() as s:
            s.add(Admin(username=f"vw_{role}", password_hash=hash_password(PW),
                        is_active=True, is_super_admin=(role == "owner"), role=role))
            await s.commit()
        c = httpx.AsyncClient(transport=transport, base_url="http://t")
        clients.append(c)
        r = await c.post("/admin/login", data={"username": f"vw_{role}", "password": PW},
                         follow_redirects=False)
        assert r.status_code == 302
        return c

    async def make_service(status: str = "active") -> int:
        async with maker() as s:
            u = User(telegram_id=1, first_name="B", language="fa")
            srv = XuiServer(name="srv", base_url="http://panel:2053", username="admin",
                            encrypted_password=crypto.encrypt("SUPERSECRETpw"),
                            panel_version="2.9.4", is_active=True, status="online")
            s.add_all([u, srv])
            await s.flush()
            ib = XuiInbound(server_id=srv.id, inbound_id=55, is_active=True)
            s.add(ib)
            await s.flush()
            p = Product(type="v2ray", title="VPN", price=1000, duration_days=30, traffic_gb=10,
                        ip_limit=1, is_active=True, is_hidden=False,
                        xui_server_id=srv.id, xui_inbound_id=ib.id)
            s.add(p)
            await s.flush()
            o = Order(order_number="DC-W-1", user_id=u.id, product_id=p.id, amount=1000,
                      final_amount=1000, status="delivered", payment_method="card_to_card")
            s.add(o)
            await s.flush()
            svc = V2RayService(user_id=u.id, order_id=o.id, product_id=p.id,
                               xui_server_id=srv.id, xui_inbound_id=ib.id,
                               client_email="dc-u1-odc-w-1", client_uuid="uuid-abcd-1234",
                               total_gb=10 * 1024 ** 3, used_gb=0, ip_limit=1, status=status)
            s.add(svc)
            await s.commit()
            return svc.id

    try:
        yield {"maker": maker, "login": login, "make_service": make_service,
               "transport": transport}
    finally:
        for c in clients:
            await c.aclose()
        app.dependency_overrides.pop(get_session, None)
        await engine.dispose()


async def test_services_requires_auth(env) -> None:
    async with httpx.AsyncClient(transport=env["transport"], base_url="http://t") as c:
        r = await c.get("/admin/v2ray-services", follow_redirects=False)
        assert r.status_code == 302


async def test_list_opens_for_admin(env) -> None:
    await env["make_service"]()
    admin = await env["login"]("admin")
    r = await admin.get("/admin/v2ray-services")
    assert r.status_code == 200 and "dc-u1-odc-w-1" in r.text


async def test_detail_opens_and_hides_password(env) -> None:
    sid = await env["make_service"]()
    admin = await env["login"]("admin")
    r = await admin.get(f"/admin/v2ray-services/{sid}")
    assert r.status_code == 200
    # The panel password / ciphertext must never render.
    assert "SUPERSECRETpw" not in r.text and "enc::" not in r.text
    # The client UUID is masked on the detail page.
    assert "uuid-abcd-1234" not in r.text


async def test_viewer_can_view_cannot_manage(env) -> None:
    sid = await env["make_service"]()
    viewer = await env["login"]("viewer")
    assert (await viewer.get("/admin/v2ray-services")).status_code == 200
    assert (await viewer.get(f"/admin/v2ray-services/{sid}")).status_code == 200
    # Manage actions are forbidden for a viewer.
    for seg in ("disable", "enable", "delete", "reset-traffic"):
        r = await viewer.post(f"/admin/v2ray-services/{sid}/{seg}", follow_redirects=False)
        assert r.status_code == 403


async def test_retry_requires_permission(env) -> None:
    sid = await env["make_service"]("failed")
    support = await env["login"]("support")  # view_services but not manage_services
    async with env["maker"]() as s:
        svc = await s.get(V2RayService, sid)
        order_id = svc.order_id
    r = await support.post(f"/admin/orders/{order_id}/retry-v2ray-provisioning",
                           follow_redirects=False)
    assert r.status_code == 403


async def test_admin_disable_succeeds(env, monkeypatch) -> None:
    sid = await env["make_service"]()
    # Stub the panel write so the route exercises the real DB + audit path.
    async def _noop(*a, **k):
        return None
    monkeypatch.setattr("app.services.xui_service.set_client_enabled", _noop)
    admin = await env["login"]("admin")
    r = await admin.post(f"/admin/v2ray-services/{sid}/disable", follow_redirects=False)
    assert r.status_code == 303 and "saved=disabled" in r.headers["location"]
    async with env["maker"]() as s:
        svc = await s.get(V2RayService, sid)
    assert svc.status == "disabled"

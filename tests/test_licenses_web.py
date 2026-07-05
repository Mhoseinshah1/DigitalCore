"""Phase 5 web: license pages auth/permission, import, secrets, actions."""
from __future__ import annotations

from collections.abc import AsyncIterator

import httpx
import pytest_asyncio
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker, create_async_engine
from sqlalchemy.pool import StaticPool

from app.core.security import hash_password
from app.database import get_session
from app.models import Admin, Base, Product
from app.services import license_service
from app.web.main import app

PW = "lic-web-1"


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
            s.add(Admin(username=f"lw_{role}", password_hash=hash_password(PW),
                        is_active=True, is_super_admin=(role == "owner"), role=role))
            await s.commit()
        c = httpx.AsyncClient(transport=transport, base_url="http://t")
        clients.append(c)
        r = await c.post("/admin/login", data={"username": f"lw_{role}", "password": PW},
                         follow_redirects=False)
        assert r.status_code == 302
        return c

    async def make_product(type_="license") -> int:
        async with maker() as s:
            kw = dict(type=type_, title="P", price=1000, is_active=True, is_hidden=False)
            if type_ == "v2ray":
                kw.update(duration_days=30, traffic_gb=10, xui_server_id=1, xui_inbound_id=1)
            p = Product(**kw)
            s.add(p)
            await s.commit()
            return p.id

    try:
        yield {"maker": maker, "login": login, "make_product": make_product,
               "transport": transport}
    finally:
        for c in clients:
            await c.aclose()
        app.dependency_overrides.pop(get_session, None)
        await engine.dispose()


async def test_licenses_requires_auth(env) -> None:
    async with httpx.AsyncClient(transport=env["transport"], base_url="http://t") as c:
        r = await c.get("/admin/licenses", follow_redirects=False)
        assert r.status_code == 302


async def test_import_requires_permission(env) -> None:
    support = await env["login"]("support")  # view_licenses but not import_licenses
    r = await support.get("/admin/licenses/import")
    assert r.status_code == 403


async def test_import_license_product_succeeds(env) -> None:
    pid = await env["make_product"]("license")
    admin = await env["login"]("admin")
    r = await admin.post("/admin/licenses/import", data={
        "product_id": str(pid),
        "raw_text": "EMAIL: a@x.com\nPASSWORD: p1\n\nEMAIL: b@x.com\nPASSWORD: p2",
    })
    assert r.status_code == 200 and ("2" in r.text)
    async with env["maker"]() as s:
        assert await license_service.count_available(s, pid) == 2


async def test_import_v2ray_product_fails(env) -> None:
    pid = await env["make_product"]("v2ray")
    admin = await env["login"]("admin")
    r = await admin.post("/admin/licenses/import", data={
        "product_id": str(pid), "raw_text": "EMAIL: a@x.com\nPASSWORD: p1",
    })
    assert r.status_code == 200
    async with env["maker"]() as s:
        # nothing imported for a non-license product
        assert await license_service.count_available(s, pid) == 0


async def test_viewer_cannot_see_password(env) -> None:
    pid = await env["make_product"]("license")
    async with env["maker"]() as s:
        lic = await license_service.add_license(s, pid, "sec@x.com", "SECRETPW", admin_id=1)
        await s.commit()
        lid = lic.id
    viewer = await env["login"]("viewer")
    r = await viewer.get(f"/admin/licenses/{lid}")
    assert r.status_code == 200
    assert "SECRETPW" not in r.text


async def test_admin_sees_password_on_detail(env) -> None:
    pid = await env["make_product"]("license")
    async with env["maker"]() as s:
        lic = await license_service.add_license(s, pid, "sec@x.com", "SECRETPW", admin_id=1)
        await s.commit()
        lid = lic.id
    admin = await env["login"]("admin")
    r = await admin.get(f"/admin/licenses/{lid}")
    assert "SECRETPW" in r.text


async def test_list_page_never_shows_password(env) -> None:
    pid = await env["make_product"]("license")
    async with env["maker"]() as s:
        await license_service.add_license(s, pid, "sec@x.com", "SECRETPW", admin_id=1)
        await s.commit()
    admin = await env["login"]("admin")
    r = await admin.get("/admin/licenses")
    assert r.status_code == 200 and "sec@x.com" in r.text and "SECRETPW" not in r.text


async def test_admin_can_mark_broken(env) -> None:
    pid = await env["make_product"]("license")
    async with env["maker"]() as s:
        lic = await license_service.add_license(s, pid, "a@x.com", "pw", admin_id=1)
        await s.commit()
        lid = lic.id
    admin = await env["login"]("admin")
    r = await admin.post(f"/admin/licenses/{lid}/mark-broken", data={"reason": "bad"},
                         follow_redirects=False)
    assert "saved=marked_broken" in r.headers["location"]
    async with env["maker"]() as s:
        lic = await license_service.get_license(s, lid)
    assert lic.status == "broken"


async def test_support_cannot_manage(env) -> None:
    pid = await env["make_product"]("license")
    async with env["maker"]() as s:
        lic = await license_service.add_license(s, pid, "a@x.com", "pw", admin_id=1)
        await s.commit()
        lid = lic.id
    support = await env["login"]("support")
    r = await support.post(f"/admin/licenses/{lid}/mark-broken", data={"reason": "x"},
                           follow_redirects=False)
    assert r.status_code == 403

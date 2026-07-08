"""Phase 11 web: reports RBAC, pages, JSON endpoints, CSV exports, and audit."""
from __future__ import annotations

from collections.abc import AsyncIterator, Awaitable, Callable
from datetime import datetime, timezone

import httpx
import pytest_asyncio
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker, create_async_engine
from sqlalchemy.pool import StaticPool

from app.core.security import hash_password
from app.database import get_session
from app.models import (
    Admin, AuditLog, Base, LicenseItem, Order, Payment, Product, User, V2RayService,
)
from app.web.main import app

PASSWORD = "rep-web-1"
NOW = datetime.now(timezone.utc)
ClientFactory = Callable[[str], Awaitable[httpx.AsyncClient]]


@pytest_asyncio.fixture
async def env() -> AsyncIterator[tuple[ClientFactory, async_sessionmaker]]:
    engine = create_async_engine("sqlite+aiosqlite://", poolclass=StaticPool,
                                 connect_args={"check_same_thread": False})
    async with engine.begin() as conn:
        await conn.run_sync(Base.metadata.create_all)
    maker = async_sessionmaker(engine, class_=AsyncSession, expire_on_commit=False)

    async def _override_session():
        async with maker() as session:
            yield session

    app.dependency_overrides[get_session] = _override_session
    transport = httpx.ASGITransport(app=app)
    clients: list[httpx.AsyncClient] = []

    async def factory(role: str) -> httpx.AsyncClient:
        username = f"rep_{role}"
        async with maker() as s:
            s.add(Admin(username=username, password_hash=hash_password(PASSWORD),
                        is_active=True, is_super_admin=(role == "owner"), role=role))
            await s.commit()
        client = httpx.AsyncClient(transport=transport, base_url="http://testserver")
        clients.append(client)
        r = await client.post("/admin/login",
                              data={"username": username, "password": PASSWORD},
                              follow_redirects=False)
        assert r.status_code == 302
        return client

    try:
        yield factory, maker
    finally:
        for c in clients:
            await c.aclose()
        app.dependency_overrides.pop(get_session, None)
        await engine.dispose()


async def _seed(maker) -> None:
    async with maker() as s:
        u = User(telegram_id=1, first_name="A", wallet_balance=5000)
        s.add(u)
        await s.flush()
        p = Product(type="license", title="Gold", price=100_000, is_active=True)
        s.add(p)
        await s.flush()
        o = Order(order_number="DC-1", user_id=u.id, product_id=p.id, amount=100_000,
                  final_amount=90_000, status="delivered", delivered_at=NOW)
        s.add(o)
        await s.flush()
        s.add(Payment(order_id=o.id, user_id=u.id, amount=90_000, status="approved"))
        s.add(LicenseItem(product_id=p.id, email="a@b.com", password="SUPERSECRETPW",
                          status="sold", sold_to_user_id=u.id, order_id=o.id, sold_at=NOW))
        s.add(V2RayService(user_id=u.id, order_id=o.id, product_id=p.id, xui_server_id=1,
                           xui_inbound_id=1, client_email="dc@x",
                           client_uuid="abcdef-UUIDSECRET-9999", total_gb=1000, used_gb=400,
                           status="active"))
        await s.commit()


# --- auth -----------------------------------------------------------------
async def test_reports_require_auth(env) -> None:
    transport = httpx.ASGITransport(app=app)
    async with httpx.AsyncClient(transport=transport, base_url="http://testserver") as anon:
        for path in ("/admin/reports", "/admin/reports/sales",
                     "/admin/reports/api/sales-by-day", "/admin/reports/export/orders.csv"):
            r = await anon.get(path, follow_redirects=False)
            assert r.status_code in (302, 307), (path, r.status_code)


async def test_owner_can_view_every_report(env) -> None:
    factory, maker = env
    await _seed(maker)
    owner = await factory("owner")
    for path in ("/admin/reports", "/admin/reports/sales", "/admin/reports/orders",
                 "/admin/reports/payments", "/admin/reports/wallet", "/admin/reports/products",
                 "/admin/reports/users", "/admin/reports/licenses", "/admin/reports/v2ray",
                 "/admin/reports/marketing", "/admin/reports/support", "/admin/reports/exports"):
        r = await owner.get(path)
        assert r.status_code == 200, (path, r.status_code)


# --- RBAC -----------------------------------------------------------------
async def test_viewer_overview_but_no_financial_or_export(env) -> None:
    factory, _ = env
    viewer = await factory("viewer")
    assert (await viewer.get("/admin/reports")).status_code == 200
    assert (await viewer.get("/admin/reports/sales", follow_redirects=False)).status_code == 403
    assert (await viewer.get("/admin/reports/users", follow_redirects=False)).status_code == 403
    r = await viewer.get("/admin/reports/export/orders.csv", follow_redirects=False)
    assert r.status_code == 403


async def test_accountant_financial_and_export(env) -> None:
    factory, maker = env
    await _seed(maker)
    acc = await factory("accountant")
    assert (await acc.get("/admin/reports/sales")).status_code == 200
    assert (await acc.get("/admin/reports/payments")).status_code == 200
    r = await acc.get("/admin/reports/export/payments.csv")
    assert r.status_code == 200 and r.headers["content-type"].startswith("text/csv")
    # but not user reports
    assert (await acc.get("/admin/reports/users", follow_redirects=False)).status_code == 403


async def test_support_user_and_service_not_financial(env) -> None:
    factory, _ = env
    sup = await factory("support")
    assert (await sup.get("/admin/reports/users")).status_code == 200
    assert (await sup.get("/admin/reports/v2ray")).status_code == 200
    assert (await sup.get("/admin/reports/sales", follow_redirects=False)).status_code == 403
    assert (await sup.get("/admin/reports/export/users.csv",
                          follow_redirects=False)).status_code == 403


# --- JSON + exports -------------------------------------------------------
async def test_json_endpoints(env) -> None:
    factory, maker = env
    await _seed(maker)
    owner = await factory("owner")
    for j in ("sales-by-day", "user-growth", "orders-by-status", "payments-by-method",
              "top-products", "v2ray-usage"):
        r = await owner.get(f"/admin/reports/api/{j}")
        assert r.status_code == 200
        assert r.headers["content-type"].startswith("application/json")
        assert isinstance(r.json(), list)
    # JSON still needs the right permission
    viewer = await factory("viewer")
    assert (await viewer.get("/admin/reports/api/sales-by-day",
                             follow_redirects=False)).status_code == 403


async def test_export_content_type_and_bom(env) -> None:
    factory, maker = env
    await _seed(maker)
    owner = await factory("owner")
    r = await owner.get("/admin/reports/export/orders.csv")
    assert r.status_code == 200
    assert r.headers["content-type"] == "text/csv; charset=utf-8"
    assert "attachment; filename=" in r.headers["content-disposition"]
    assert r.text.startswith("﻿")


async def test_date_filters_do_not_error(env) -> None:
    factory, maker = env
    await _seed(maker)
    owner = await factory("owner")
    for q in ("preset=today", "preset=last_7_days", "preset=this_month",
              "start=2026-01-01&end=2026-12-31"):
        r = await owner.get(f"/admin/reports/sales?{q}")
        assert r.status_code == 200, (q, r.status_code)


# --- dashboard ------------------------------------------------------------
async def test_dashboard_renders_with_analytics(env) -> None:
    factory, maker = env
    await _seed(maker)
    owner = await factory("owner")
    r = await owner.get("/admin")
    assert r.status_code == 200


# --- security + audit -----------------------------------------------------
async def test_exports_leak_no_secrets(env) -> None:
    factory, maker = env
    await _seed(maker)
    owner = await factory("owner")
    lic = (await owner.get("/admin/reports/export/licenses.csv")).text
    assert "SUPERSECRETPW" not in lic and "password" not in lic.splitlines()[0].lower()
    v2 = (await owner.get("/admin/reports/export/v2ray-services.csv")).text
    assert "UUIDSECRET" not in v2 and "****9999" in v2


async def test_audit_rows_created(env) -> None:
    factory, maker = env
    await _seed(maker)
    owner = await factory("owner")
    await owner.get("/admin/reports/sales")
    await owner.get("/admin/reports/export/orders.csv")
    async with maker() as s:
        actions = [a.action for a in (await s.execute(
            select(AuditLog).where(AuditLog.action.like("report.%")))).scalars().all()]
    assert "report.financial_viewed" in actions
    assert "report.export_created" in actions

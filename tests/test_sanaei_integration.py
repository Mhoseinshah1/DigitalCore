"""Integration tests for the rebuilt Sanaei 3x-ui wiring.

Covers the layers above the raw API client: the adapter's credential/auth-mode
decisions (`build_client`), the service-level connection test + inbound sync, the
product dry-run validator, and the admin server-form field parsing. All HTTP is
mocked with httpx.MockTransport; no real panel or network is touched.
"""
from __future__ import annotations

import httpx

from app.core import crypto
from app.models.product import Product
from app.models.xui_inbound import XuiInbound
from app.models.xui_server import XuiServer
from app.services import v2ray_service, xui_service
from app.services.sanaei_adapter import build_client
from app.web.views import _server_form_values

BASE_URL = "https://panel.example:2053"


async def _noop_sleep(_):
    return None


def _status_ok():
    return httpx.Response(200, json={
        "success": True, "msg": "",
        "obj": {"version": "2.9.4", "xray": {"version": "1.8.11"}},
    })


def _inbounds_ok(items):
    return httpx.Response(200, json={"success": True, "msg": "", "obj": items})


async def _make_server(db_session, **kw) -> XuiServer:
    defaults = dict(
        name="srv", base_url=BASE_URL, panel_type="3x-ui", panel_version="2.9.4",
        status="unknown", is_active=True,
    )
    defaults.update(kw)
    server = XuiServer(**defaults)
    db_session.add(server)
    await db_session.flush()
    return server


# --------------------------------------------------------------------------
# Adapter: build_client honours the stored auth mode + credentials
# --------------------------------------------------------------------------
async def test_build_client_prefers_api_token(db_session) -> None:
    server = await _make_server(
        db_session, auth_mode="api_token",
        encrypted_api_token=crypto.encrypt("tok-123"),
        encrypted_password=crypto.encrypt("pw"), username="admin",
    )
    client = build_client(server)
    try:
        assert client.auth_mode == "api_token"
        assert client._headers().get("Authorization") == "Bearer tok-123"
    finally:
        await client.aclose()


async def test_build_client_password_mode_ignores_token(db_session) -> None:
    # A stale token must be ignored when the admin picked password auth.
    server = await _make_server(
        db_session, auth_mode="password",
        encrypted_api_token=crypto.encrypt("stale"),
        encrypted_password=crypto.encrypt("pw"), username="admin",
    )
    client = build_client(server)
    try:
        assert client.auth_mode == "password"
        assert client._headers().get("Authorization") is None
    finally:
        await client.aclose()


async def test_build_client_carries_tls_and_timeout(db_session) -> None:
    server = await _make_server(
        db_session, auth_mode="api_token",
        encrypted_api_token=crypto.encrypt("t"), tls_verify=False, timeout_seconds=7,
    )
    client = build_client(server)
    try:
        assert client._client.timeout.read == 7.0
    finally:
        await client.aclose()


# --------------------------------------------------------------------------
# Service: connection test records diagnostics (token / Bearer path)
# --------------------------------------------------------------------------
async def test_connection_records_versions_with_token(db_session) -> None:
    server = await _make_server(
        db_session, auth_mode="api_token", encrypted_api_token=crypto.encrypt("tok"),
    )
    seen = {"auth": None, "login": False}

    def handler(request: httpx.Request) -> httpx.Response:
        if request.url.path.endswith("/login"):
            seen["login"] = True
            return httpx.Response(200, json={"success": True})
        seen["auth"] = request.headers.get("Authorization")
        if request.url.path.endswith("/panel/api/server/status"):
            return _status_ok()
        if request.url.path.endswith("/panel/api/inbounds/list"):
            return _inbounds_ok([{"id": 1}, {"id": 2}])
        return httpx.Response(404)

    result = await xui_service.test_connection(
        db_session, server, transport=httpx.MockTransport(handler), sleep=_noop_sleep
    )
    assert result["ok"] is True
    assert result["inbound_count"] == 2
    assert result["panel_version"] == "2.9.4"
    assert result["xray_version"] == "1.8.11"
    assert seen["login"] is False  # token auth never logs in
    assert seen["auth"] == "Bearer tok"
    assert server.status == "online"
    assert server.panel_version == "2.9.4"
    assert server.xray_version == "1.8.11"


# --------------------------------------------------------------------------
# Service: sync_inbounds upserts and never deletes missing inbounds
# --------------------------------------------------------------------------
async def test_sync_inbounds_upsert_keeps_missing(db_session) -> None:
    server = await _make_server(
        db_session, auth_mode="api_token", encrypted_api_token=crypto.encrypt("tok"),
    )
    # A pre-existing inbound the panel will NOT return this round.
    stale = XuiInbound(server_id=server.id, inbound_id=99, remark="old", is_active=True)
    db_session.add(stale)
    await db_session.flush()

    stream = '{"network": "ws", "security": "reality"}'

    def handler(request: httpx.Request) -> httpx.Response:
        if request.url.path.endswith("/panel/api/inbounds/list"):
            return _inbounds_ok([
                {"id": 1, "remark": "in-1", "protocol": "vless", "port": 443,
                 "tag": "inbound-1", "enable": True, "streamSettings": stream},
            ])
        return httpx.Response(404)

    n = await xui_service.sync_inbounds(
        db_session, server, transport=httpx.MockTransport(handler), sleep=_noop_sleep
    )
    assert n == 1
    rows = {r.inbound_id: r for r in await xui_server_rows(db_session, server.id)}
    # The new inbound was captured with normalized stream fields + raw_json...
    assert rows[1].network == "ws"
    assert rows[1].security == "reality"
    assert rows[1].tag == "inbound-1"
    assert rows[1].raw_json and rows[1].synced_at is not None
    # ...and the stale one still exists (sync never deletes).
    assert 99 in rows


async def xui_server_rows(db_session, server_id):
    from sqlalchemy import select
    res = await db_session.execute(
        select(XuiInbound).where(XuiInbound.server_id == server_id)
    )
    return list(res.scalars().all())


# --------------------------------------------------------------------------
# Dry-run product validator
# --------------------------------------------------------------------------
async def test_dry_run_unbound_product_fails(db_session) -> None:
    product = Product(type="v2ray", title="p", price=1000,
                      duration_days=30, traffic_gb=50)
    db_session.add(product)
    await db_session.flush()

    report = await v2ray_service.dry_run_validate_product(db_session, product.id)
    assert report["ok"] is False
    failed = {c["check"] for c in report["checks"] if not c["ok"]}
    assert "product_bound" in failed


async def test_dry_run_valid_product_passes(db_session) -> None:
    server = await _make_server(
        db_session, auth_mode="api_token", encrypted_api_token=crypto.encrypt("tok"),
        public_sub_base_url="https://sub.example:2096",
    )
    inbound = XuiInbound(server_id=server.id, inbound_id=1, is_active=True)
    db_session.add(inbound)
    await db_session.flush()
    product = Product(type="v2ray", title="p", price=1000, duration_days=30,
                      traffic_gb=50, xui_server_id=server.id, xui_inbound_id=inbound.id)
    db_session.add(product)
    await db_session.flush()

    report = await v2ray_service.dry_run_validate_product(db_session, product.id)
    assert report["ok"] is True, [c for c in report["checks"] if not c["ok"]]
    assert all(c["ok"] for c in report["checks"])


async def test_dry_run_with_live_probe(db_session) -> None:
    server = await _make_server(
        db_session, auth_mode="api_token", encrypted_api_token=crypto.encrypt("tok"),
        public_sub_base_url="https://sub.example:2096",
    )
    inbound = XuiInbound(server_id=server.id, inbound_id=1, is_active=True)
    db_session.add(inbound)
    await db_session.flush()
    product = Product(type="v2ray", title="p", price=1000, duration_days=30,
                      traffic_gb=50, xui_server_id=server.id, xui_inbound_id=inbound.id)
    db_session.add(product)
    await db_session.flush()

    def handler(request: httpx.Request) -> httpx.Response:
        if request.url.path.endswith("/panel/api/inbounds/list"):
            return _inbounds_ok([{"id": 1}])
        if request.url.path.endswith("/panel/api/server/status"):
            return _status_ok()
        return httpx.Response(404)

    report = await v2ray_service.dry_run_validate_product(
        db_session, product.id, test_connection=True,
        transport=httpx.MockTransport(handler), sleep=_noop_sleep,
    )
    assert report["connection"]["ok"] is True
    assert report["ok"] is True


async def test_dry_run_missing_product(db_session) -> None:
    report = await v2ray_service.dry_run_validate_product(db_session, 999999)
    assert report["ok"] is False
    assert report["checks"][0]["check"] == "product_exists"


# --------------------------------------------------------------------------
# Admin form parsing
# --------------------------------------------------------------------------
def test_server_form_values_parses_new_fields() -> None:
    vals = _server_form_values({
        "name": " srv ", "base_url": "http://h:2053/", "username": "admin",
        "password": "pw", "api_token": " tok ", "auth_mode": "api_token",
        "web_base_path": "panel", "public_sub_base_url": "https://sub:2096",
        "subscription_path": "/sub/", "timeout_seconds": "9",
        "tls_verify": "on", "is_active": "on",
    })
    assert vals["auth_mode"] == "api_token"
    assert vals["api_token"] == "tok"
    assert vals["web_base_path"] == "panel"
    assert vals["public_sub_base_url"] == "https://sub:2096"
    assert vals["timeout_seconds"] == 9
    assert vals["tls_verify"] is True
    assert vals["is_active"] is True


def test_server_form_values_defaults_when_blank() -> None:
    vals = _server_form_values({"name": "srv", "base_url": "http://h"})
    assert vals["auth_mode"] is None          # keep existing / auto-resolve
    assert vals["api_token"] is None          # empty = keep stored secret
    assert vals["password"] is None
    assert vals["timeout_seconds"] is None    # empty = keep / default
    assert vals["tls_verify"] is False        # checkbox absent
    assert vals["is_active"] is False

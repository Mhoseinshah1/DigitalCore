"""Tests for the automatic 3X-UI inbound sync service.

Everything above the raw API client: normalize_inbound's tolerance of the many
shapes a panel returns, the create/update/mark-inactive upsert of
sync_server_inbounds, and the product-binding helpers (list_active_synced_inbounds
/ get_best_default_inbound). All HTTP is mocked with httpx.MockTransport.
"""
from __future__ import annotations

import httpx

from app.core import crypto
from app.models.xui_inbound import XuiInbound
from app.models.xui_server import XuiServer
from app.services import xui_inbound_sync_service as sync_svc

BASE_URL = "https://panel.example:2053"


def _inbounds_ok(items):
    return httpx.Response(200, json={"success": True, "msg": "", "obj": items})


async def _make_server(db_session, **kw) -> XuiServer:
    defaults = dict(
        name="srv", base_url=BASE_URL, status="unknown", is_active=True,
        auth_mode="api_token", encrypted_api_token=crypto.encrypt("tok"),
    )
    defaults.update(kw)
    server = XuiServer(**defaults)
    db_session.add(server)
    await db_session.flush()
    return server


# ==========================================================================
# normalize_inbound: never raises, tolerates every stream shape
# ==========================================================================
def test_normalize_stream_settings_as_json_string() -> None:
    row = sync_svc.normalize_inbound({
        "id": 3, "remark": "R", "protocol": "VLESS", "port": "443",
        "tag": "inbound-3", "enable": True,
        "streamSettings": '{"network": "ws", "security": "reality"}',
    })
    assert row["remote_inbound_id"] == 3
    assert row["protocol"] == "vless"        # lower-cased
    assert row["port"] == 443                 # coerced from str
    assert row["network"] == "ws"
    assert row["security"] == "reality"
    assert row["tag"] == "inbound-3"
    assert row["enable"] is True
    assert '"remark": "R"' in row["raw_json"]   # full inbound preserved as JSON


def test_normalize_stream_settings_as_object() -> None:
    row = sync_svc.normalize_inbound({
        "id": 4, "protocol": "trojan", "port": 8443,
        "streamSettings": {"network": "grpc", "security": "tls"},
    })
    assert row["network"] == "grpc"
    assert row["security"] == "tls"


def test_normalize_missing_and_malformed_stream() -> None:
    # No streamSettings at all → network/security are None, still usable.
    row = sync_svc.normalize_inbound({"id": 5, "protocol": "shadowsocks"})
    assert row["network"] is None and row["security"] is None
    # Malformed JSON string must not raise.
    bad = sync_svc.normalize_inbound({"id": 6, "streamSettings": "{not json"})
    assert bad["remote_inbound_id"] == 6
    assert bad["network"] is None


def test_normalize_non_dict_returns_empty() -> None:
    assert sync_svc.normalize_inbound("nope") == {}
    assert sync_svc.normalize_inbound(None) == {}
    assert sync_svc.normalize_inbound([1, 2]) == {}


# ==========================================================================
# sync_server_inbounds: create / update / mark-missing-inactive
# ==========================================================================
async def test_sync_creates_and_marks_missing_inactive(db_session) -> None:
    server = await _make_server(db_session)
    # An inbound the panel will NOT return this round (should be marked inactive).
    stale = XuiInbound(server_id=server.id, inbound_id=99, remark="old", is_active=True)
    db_session.add(stale)
    await db_session.flush()

    def handler(request: httpx.Request) -> httpx.Response:
        if request.url.path.endswith("/panel/api/inbounds/list"):
            return _inbounds_ok([
                {"id": 1, "remark": "in-1", "protocol": "vmess", "port": 2053,
                 "enable": True, "streamSettings": '{"network": "tcp"}'},
                {"id": 2, "remark": "in-2", "protocol": "vless", "port": 443,
                 "enable": False},
            ])
        return httpx.Response(404)

    result = await sync_svc.sync_server_inbounds(
        db_session, server.id, transport=httpx.MockTransport(handler))

    assert result.success is True
    assert result.created_count == 2
    assert result.total_remote_count == 2
    assert result.disabled_count == 1           # the stale #99
    assert result.synced_at is not None
    assert server.status == "active" and server.last_error is None

    rows = {r.inbound_id: r for r in await _rows(db_session, server.id)}
    assert rows[1].protocol == "vmess" and rows[1].network == "tcp"
    assert rows[1].is_active is True            # seeded from panel enable
    assert rows[2].is_active is False           # panel enable=False
    assert rows[99].is_active is False          # vanished → marked inactive
    assert rows[99].remark == "old"             # kept for history, never deleted


async def test_sync_update_preserves_admin_disable(db_session) -> None:
    server = await _make_server(db_session)
    # Admin has locally disabled a still-present inbound for sales.
    row = XuiInbound(server_id=server.id, inbound_id=1, remark="stale", is_active=False)
    db_session.add(row)
    await db_session.flush()

    def handler(request: httpx.Request) -> httpx.Response:
        if request.url.path.endswith("/panel/api/inbounds/list"):
            return _inbounds_ok([{"id": 1, "remark": "fresh", "protocol": "vless",
                                  "port": 443, "enable": True}])
        return httpx.Response(404)

    result = await sync_svc.sync_server_inbounds(
        db_session, server.id, transport=httpx.MockTransport(handler))
    assert result.updated_count == 1 and result.created_count == 0
    rows = {r.inbound_id: r for r in await _rows(db_session, server.id)}
    assert rows[1].remark == "fresh"            # details refreshed
    assert rows[1].enable_from_panel is True    # panel mirror refreshed
    assert rows[1].is_active is False           # admin's local disable preserved


async def test_sync_failure_records_error_on_server(db_session) -> None:
    server = await _make_server(db_session)

    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(401, text="unauthorized")

    result = await sync_svc.sync_server_inbounds(
        db_session, server.id, transport=httpx.MockTransport(handler))
    assert result.success is False
    assert result.error_message
    assert server.status == "error"
    assert server.last_error


async def test_sync_missing_server_is_safe(db_session) -> None:
    result = await sync_svc.sync_server_inbounds(db_session, 999999)
    assert result.success is False
    assert result.error_message == "server not found"


# ==========================================================================
# Binding helpers
# ==========================================================================
async def test_list_active_and_best_default(db_session) -> None:
    server = await _make_server(db_session)
    db_session.add_all([
        XuiInbound(server_id=server.id, inbound_id=1, is_active=True),
        XuiInbound(server_id=server.id, inbound_id=2, is_active=False),
    ])
    await db_session.flush()

    active = await sync_svc.list_active_synced_inbounds(db_session, server.id)
    assert [i.inbound_id for i in active] == [1]
    # Exactly one active → auto-selectable.
    best = await sync_svc.get_best_default_inbound(db_session, server.id)
    assert best is not None and best.inbound_id == 1

    # Add a second active inbound → no single default any more.
    db_session.add(XuiInbound(server_id=server.id, inbound_id=3, is_active=True))
    await db_session.flush()
    assert await sync_svc.get_best_default_inbound(db_session, server.id) is None


async def test_sync_all_active_servers(db_session) -> None:
    s1 = await _make_server(db_session, name="a")
    await _make_server(db_session, name="b", is_active=False)  # skipped

    def handler(request: httpx.Request) -> httpx.Response:
        if request.url.path.endswith("/panel/api/inbounds/list"):
            return _inbounds_ok([{"id": 1, "enable": True}])
        return httpx.Response(404)

    results = await sync_svc.sync_all_active_servers(
        db_session, transport=httpx.MockTransport(handler))
    assert len(results) == 1                     # only the active server
    assert results[0].server_id == s1.id
    assert results[0].success is True


async def _rows(db_session, server_id):
    from sqlalchemy import select
    res = await db_session.execute(
        select(XuiInbound).where(XuiInbound.server_id == server_id))
    return list(res.scalars().all())

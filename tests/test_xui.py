"""3X-UI integration — fully mocked (no real network).

Covers the low-level HTTP client (login, 401 re-login, retry/backoff), the
version-selectable registry, encrypted-at-rest credentials, service-level
test-connection / sync-inbounds, add-client verify-after-write, and i18n parity
for the admin-facing server strings. Every HTTP call goes through
httpx.MockTransport; retry backoff uses an injected sleep so nothing waits.
"""
from __future__ import annotations

import json

import httpx
import pytest
from sqlalchemy import select

from app.core import crypto
from app.i18n.en import CATALOG as EN
from app.i18n.fa import CATALOG as FA
from app.models.xui_inbound import XuiInbound
from app.services import xui_service
from app.xui.adapters.xui_2_9_4 import Xui294Adapter
from app.xui.adapters.xui_latest import XuiLatestAdapter
from app.xui.client import XuiHttpClient
from app.xui.exceptions import (
    XuiApiError,
    XuiNetworkError,
    XuiVerificationError,
)
from app.xui.registry import get_adapter_class
from app.xui.schemas import ClientAdd

BASE_URL = "http://panel.example:2053"


def _login_ok() -> httpx.Response:
    return httpx.Response(200, json={"success": True, "msg": "", "obj": None})


def _login_fail(msg: str = "wrong credentials") -> httpx.Response:
    return httpx.Response(200, json={"success": False, "msg": msg, "obj": None})


def _envelope(obj: object) -> httpx.Response:
    return httpx.Response(200, json={"success": True, "msg": "", "obj": obj})


async def _noop_sleep(_delay: float) -> None:  # pragma: no cover - trivial
    return None


def _make_client(handler, **kwargs) -> XuiHttpClient:
    return XuiHttpClient(
        base_url=BASE_URL,
        username="admin",
        password="pw",
        transport=httpx.MockTransport(handler),
        sleep=_noop_sleep,
        **kwargs,
    )


# --------------------------------------------------------------------------
# Low-level client: login + 401 re-login
# --------------------------------------------------------------------------
async def test_login_success_and_relogin_on_401() -> None:
    calls = {"login": 0, "list": 0}

    def handler(request: httpx.Request) -> httpx.Response:
        path = request.url.path
        if path.endswith("/login"):
            calls["login"] += 1
            return _login_ok()
        if path.endswith("/panel/api/inbounds/list"):
            calls["list"] += 1
            if calls["list"] == 1:
                return httpx.Response(401, text="unauthorized")
            return _envelope([])
        return httpx.Response(404)

    http = _make_client(handler)
    obj = await http.request("GET", "/panel/api/inbounds/list")
    await http.aclose()

    assert obj == []
    # One initial login + exactly one re-login after the 401.
    assert calls["login"] == 2
    assert calls["list"] == 2


async def test_relogin_gives_up_after_second_401() -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        if request.url.path.endswith("/login"):
            return _login_ok()
        return httpx.Response(401, text="unauthorized")

    http = _make_client(handler)
    with pytest.raises(Exception) as excinfo:
        await http.request("GET", "/panel/api/inbounds/list")
    await http.aclose()
    # Still unauthorized after the single permitted re-login -> XuiAuthError.
    assert "unauthorized" in str(excinfo.value).lower()


# --------------------------------------------------------------------------
# Low-level client: retry / backoff
# --------------------------------------------------------------------------
async def test_retry_on_transient_5xx_then_success() -> None:
    sleeps: list[float] = []

    async def record_sleep(delay: float) -> None:
        sleeps.append(delay)

    calls = {"list": 0}

    def handler(request: httpx.Request) -> httpx.Response:
        path = request.url.path
        if path.endswith("/login"):
            return _login_ok()
        if path.endswith("/panel/api/inbounds/list"):
            calls["list"] += 1
            if calls["list"] < 3:
                return httpx.Response(503, text="busy")
            return _envelope([])
        return httpx.Response(404)

    http = XuiHttpClient(
        base_url=BASE_URL,
        username="admin",
        password="pw",
        transport=httpx.MockTransport(handler),
        max_retries=3,
        backoff_base=0.5,
        sleep=record_sleep,
    )
    obj = await http.request("GET", "/panel/api/inbounds/list")
    await http.aclose()

    assert obj == []
    assert calls["list"] == 3
    # Exponential backoff: base*2**0 then base*2**1.
    assert sleeps == [0.5, 1.0]


async def test_persistent_network_error_raises() -> None:
    sleeps: list[float] = []

    async def record_sleep(delay: float) -> None:
        sleeps.append(delay)

    def handler(request: httpx.Request) -> httpx.Response:
        if request.url.path.endswith("/login"):
            return _login_ok()
        raise httpx.ConnectError("connection refused")

    http = XuiHttpClient(
        base_url=BASE_URL,
        username="admin",
        password="pw",
        transport=httpx.MockTransport(handler),
        max_retries=3,
        backoff_base=0.1,
        sleep=record_sleep,
    )
    with pytest.raises(XuiNetworkError):
        await http.request("GET", "/panel/api/inbounds/list")
    await http.aclose()
    # Two backoff waits before the third and final attempt raises.
    assert len(sleeps) == 2


# --------------------------------------------------------------------------
# Registry: version selection
# --------------------------------------------------------------------------
def test_registry_selects_adapter_by_version() -> None:
    assert get_adapter_class("3x-ui", "2.9.4") is Xui294Adapter
    assert get_adapter_class("3x-ui", "latest") is XuiLatestAdapter
    # `latest` currently inherits every operation from 2.9.4.
    assert issubclass(XuiLatestAdapter, Xui294Adapter)


def test_registry_rejects_unknown_version() -> None:
    with pytest.raises(XuiApiError):
        get_adapter_class("3x-ui", "9.9.9")
    with pytest.raises(XuiApiError):
        get_adapter_class("marzban", "latest")


# --------------------------------------------------------------------------
# Service: encrypted credentials at rest
# --------------------------------------------------------------------------
async def test_add_server_encrypts_password(db_session) -> None:
    server = await xui_service.add_server(
        db_session,
        name="Frankfurt",
        base_url="http://panel.example:2053/",
        username="admin",
        password="super-secret",
        panel_version="2.9.4",
        api_token="tok-123",
    )
    # Never stored in plaintext; round-trips back through crypto.
    assert server.encrypted_password != "super-secret"
    assert server.encrypted_password.startswith("enc::")
    assert crypto.decrypt(server.encrypted_password) == "super-secret"
    assert server.encrypted_api_token and server.encrypted_api_token.startswith("enc::")
    assert crypto.decrypt(server.encrypted_api_token) == "tok-123"
    # Trailing slash on base_url is normalised away.
    assert server.base_url == "http://panel.example:2053"


async def test_add_server_rejects_unknown_version(db_session) -> None:
    with pytest.raises(ValueError):
        await xui_service.add_server(
            db_session,
            name="bad",
            base_url=BASE_URL,
            username="admin",
            password="pw",
            panel_version="0.0.0",
        )


# --------------------------------------------------------------------------
# Service: test_connection
# --------------------------------------------------------------------------
async def _make_server(db_session):
    return await xui_service.add_server(
        db_session,
        name="srv",
        base_url=BASE_URL,
        username="admin",
        password="pw",
        panel_version="2.9.4",
    )


async def test_test_connection_online(db_session) -> None:
    server = await _make_server(db_session)

    def handler(request: httpx.Request) -> httpx.Response:
        if request.url.path.endswith("/login"):
            return _login_ok()
        return httpx.Response(404)

    result = await xui_service.test_connection(
        db_session, server, transport=httpx.MockTransport(handler), sleep=_noop_sleep
    )
    assert result["ok"] is True
    assert result["status"] == "online"
    assert server.status == "online"
    assert server.last_health_check is not None


async def test_test_connection_auth_error(db_session) -> None:
    server = await _make_server(db_session)

    def handler(request: httpx.Request) -> httpx.Response:
        if request.url.path.endswith("/login"):
            return _login_fail()
        return httpx.Response(404)

    result = await xui_service.test_connection(
        db_session, server, transport=httpx.MockTransport(handler), sleep=_noop_sleep
    )
    assert result["ok"] is False
    assert result["status"] == "auth_error"
    assert server.status == "auth_error"


# --------------------------------------------------------------------------
# Service: sync_inbounds is idempotent
# --------------------------------------------------------------------------
async def test_sync_inbounds_idempotent(db_session) -> None:
    server = await _make_server(db_session)
    inbounds_obj = [
        {
            "id": 1,
            "remark": "vless-in",
            "protocol": "vless",
            "port": 443,
            "enable": True,
            "settings": json.dumps({"clients": []}),
        },
        {
            "id": 2,
            "remark": "vmess-in",
            "protocol": "vmess",
            "port": 8443,
            "enable": False,
            "settings": "{}",
        },
    ]

    def handler(request: httpx.Request) -> httpx.Response:
        path = request.url.path
        if path.endswith("/login"):
            return _login_ok()
        if path.endswith("/panel/api/inbounds/list"):
            return _envelope(inbounds_obj)
        return httpx.Response(404)

    transport = httpx.MockTransport(handler)
    n1 = await xui_service.sync_inbounds(db_session, server, transport=transport, sleep=_noop_sleep)
    n2 = await xui_service.sync_inbounds(db_session, server, transport=transport, sleep=_noop_sleep)
    assert n1 == 2 and n2 == 2

    rows = (
        await db_session.execute(
            select(XuiInbound).where(XuiInbound.server_id == server.id).order_by(XuiInbound.inbound_id)
        )
    ).scalars().all()
    # Upsert keyed on (server_id, inbound_id): two syncs -> two rows, no dupes.
    assert [r.inbound_id for r in rows] == [1, 2]
    assert rows[0].remark == "vless-in" and rows[0].is_active is True
    assert rows[1].is_active is False
    assert server.status == "online"


# --------------------------------------------------------------------------
# Service: add_client + verify-after-write
# --------------------------------------------------------------------------
def _inbound_with_client(total_gb: int) -> dict:
    settings = json.dumps(
        {
            "clients": [
                {
                    "id": "uuid-1",
                    "email": "buyer@example.com",
                    "enable": True,
                    "expiryTime": 0,
                    "totalGB": total_gb,
                    "limitIp": 2,
                }
            ]
        }
    )
    return {
        "id": 7,
        "remark": "in",
        "protocol": "vless",
        "port": 443,
        "enable": True,
        "settings": settings,
    }


def _add_client_handler(inbound_obj: dict):
    def handler(request: httpx.Request) -> httpx.Response:
        path = request.url.path
        if path.endswith("/login"):
            return _login_ok()
        if path.endswith("/panel/api/inbounds/addClient"):
            return _envelope(None)
        if "/panel/api/inbounds/get/7" in path:
            return _envelope(inbound_obj)
        return httpx.Response(404)

    return handler


async def test_add_client_verify_after_write_ok(db_session) -> None:
    server = await _make_server(db_session)
    client = ClientAdd(
        email="buyer@example.com",
        uuid="uuid-1",
        enable=True,
        expiry_time=0,
        total_gb=1073741824,
        limit_ip=2,
    )
    transport = httpx.MockTransport(_add_client_handler(_inbound_with_client(1073741824)))
    result = await xui_service.add_client(
        server, 7, client, transport=transport, sleep=_noop_sleep
    )
    assert result is not None
    assert result.email == "buyer@example.com"
    assert result.total_gb == 1073741824


async def test_add_client_verify_after_write_mismatch_raises(db_session) -> None:
    server = await _make_server(db_session)
    client = ClientAdd(
        email="buyer@example.com",
        uuid="uuid-1",
        enable=True,
        expiry_time=0,
        total_gb=1073741824,
        limit_ip=2,
    )
    # The panel echoes back a different quota -> verify-after-write must fail loudly.
    transport = httpx.MockTransport(_add_client_handler(_inbound_with_client(999)))
    with pytest.raises(XuiVerificationError):
        await xui_service.add_client(server, 7, client, transport=transport, sleep=_noop_sleep)


async def test_add_client_verify_missing_client_raises(db_session) -> None:
    server = await _make_server(db_session)
    client = ClientAdd(email="ghost@example.com", uuid="uuid-9", enable=True)
    # Inbound has no matching client -> not found after write.
    transport = httpx.MockTransport(_add_client_handler(_inbound_with_client(1073741824)))
    with pytest.raises(XuiVerificationError):
        await xui_service.add_client(server, 7, client, transport=transport, sleep=_noop_sleep)


# --------------------------------------------------------------------------
# i18n parity for the admin-facing 3X-UI strings
# --------------------------------------------------------------------------
def test_xui_i18n_keys_present_in_both_catalogs() -> None:
    xui_keys = [k for k in EN if k.startswith(("xui.", "servers.", "web.servers."))]
    assert xui_keys, "expected xui.* / servers.* strings in the catalog"
    for key in xui_keys:
        assert key in FA, f"key {key!r} missing from the fa catalog"
    # Representative keys are non-empty in both languages.
    for key in ("xui.test.ok", "servers.pick_version", "web.servers.title"):
        assert EN[key] and FA[key]

"""Mocked-transport tests for the rebuilt Sanaei 3x-ui API client."""
from __future__ import annotations

import json

import httpx
import pytest

from app.services.sanaei_api_client import (
    SanaeiApiClient,
    bytes_for_gb,
    normalize_base,
)
from app.xui.exceptions import XuiApiError, XuiAuthError, XuiNetworkError


def _ok(obj):
    return httpx.Response(200, json={"success": True, "msg": "", "obj": obj})


def _fail(msg="nope"):
    return httpx.Response(200, json={"success": False, "msg": msg, "obj": None})


async def _nosleep(_):  # never actually wait in tests
    return None


def make_client(handler, **kw) -> SanaeiApiClient:
    return SanaeiApiClient(
        "https://panel.example:2053", web_base_path="secret",
        transport=httpx.MockTransport(handler), sleep=_nosleep, **kw)


# --------------------------------------------------------------------------
def test_normalize_base() -> None:
    assert normalize_base("https://h:2053/", "abc/") == "https://h:2053/abc"
    assert normalize_base("https://h:2053", None) == "https://h:2053"


def test_bytes_for_gb() -> None:
    assert bytes_for_gb(1) == 1024 ** 3
    assert bytes_for_gb(0) == 0


async def test_bearer_header_sent_and_no_login() -> None:
    seen = {}

    def handler(req: httpx.Request) -> httpx.Response:
        seen["auth"] = req.headers.get("Authorization")
        seen["path"] = req.url.path
        return _ok([])

    async with make_client(handler, api_token="secret-tok") as c:
        await c.list_inbounds()
    assert seen["auth"] == "Bearer secret-tok"
    assert seen["path"] == "/secret/panel/api/inbounds/list"  # base path applied


async def test_cookie_login_fallback() -> None:
    calls = []

    def handler(req: httpx.Request) -> httpx.Response:
        calls.append(req.url.path)
        if req.url.path.endswith("/login"):
            return httpx.Response(200, json={"success": True}, headers={"Set-Cookie": "session=x"})
        return _ok([{"id": 1}])

    async with make_client(handler, username="admin", password="pw") as c:
        inbounds = await c.list_inbounds()
    assert any(p.endswith("/login") for p in calls)  # logged in first
    assert inbounds == [{"id": 1}]


async def test_invalid_token_raises_auth_error() -> None:
    def handler(req: httpx.Request) -> httpx.Response:
        return httpx.Response(401, text="unauthorized")

    with pytest.raises(XuiAuthError):
        async with make_client(handler, api_token="bad") as c:
            await c.list_inbounds()


async def test_server_status_and_test_connection() -> None:
    def handler(req: httpx.Request) -> httpx.Response:
        if req.url.path.endswith("/server/status"):
            return _ok({"version": "2.9.4", "xray": {"version": "1.8.4", "state": "running"}})
        if req.url.path.endswith("/inbounds/list"):
            return _ok([{"id": 1}, {"id": 2}])
        return _ok(None)

    async with make_client(handler, api_token="t") as c:
        result = await c.test_connection()
    assert result["ok"] is True
    assert result["inbound_count"] == 2
    assert result["panel_version"] == "2.9.4"
    assert result["xray_version"] == "1.8.4"
    assert result["auth_mode"] == "api_token"


async def test_add_client_uses_new_endpoint_with_bytes_and_ms() -> None:
    captured = {}

    def handler(req: httpx.Request) -> httpx.Response:
        if req.url.path.endswith("/clients/add"):
            captured["body"] = json.loads(req.content)
            captured["path"] = req.url.path
            return _ok(None)
        return _ok(None)

    client = {"email": "dc-1", "totalGB": bytes_for_gb(50),
              "expiryTime": 1893456000000, "enable": True, "limitIp": 0, "tgId": 42}
    async with make_client(handler, api_token="t") as c:
        await c.add_client(7, client)
    assert captured["path"].endswith("/panel/api/clients/add")
    assert captured["body"]["inboundIds"] == [7]
    assert captured["body"]["client"]["totalGB"] == 50 * 1024 ** 3
    assert captured["body"]["client"]["expiryTime"] == 1893456000000


async def test_add_client_falls_back_to_legacy_on_404() -> None:
    seen = {}

    def handler(req: httpx.Request) -> httpx.Response:
        if req.url.path.endswith("/clients/add"):
            return httpx.Response(404, text="not found")
        if req.url.path.endswith("/inbounds/addClient"):
            # legacy form-encoded body with settings JSON
            seen["form"] = dict(httpx.QueryParams(req.content.decode()))
            return _ok(None)
        return _ok(None)

    async with make_client(handler, api_token="t") as c:
        await c.add_client(3, {"email": "dc-x", "totalGB": 0, "enable": True})
    assert seen["form"]["id"] == "3"
    assert "clients" in seen["form"]["settings"]


async def test_delete_client_keep_traffic_param() -> None:
    seen = {}

    def handler(req: httpx.Request) -> httpx.Response:
        if "/clients/del/" in req.url.path:
            seen["q"] = dict(req.url.params)
            return _ok(None)
        return _ok(None)

    async with make_client(handler, api_token="t") as c:
        await c.delete_client(1, "dc-x", keep_traffic=True)
    assert seen["q"]["keepTraffic"] == "1"


async def test_all_links_and_subscription_urls() -> None:
    def handler(req: httpx.Request) -> httpx.Response:
        if req.url.path.endswith("/allLinks"):
            return _ok(["vless://..."])
        return _ok(None)

    c = SanaeiApiClient("https://panel:2053", api_token="t",
                        subscription_base_url="https://sub.example:2096",
                        transport=httpx.MockTransport(handler), sleep=_nosleep)
    try:
        links = await c.get_all_links()
        assert links == ["vless://..."]
        assert c.build_subscription_url("SUB123") == "https://sub.example:2096/sub/SUB123"
        assert c.build_json_subscription_url("SUB123") == "https://sub.example:2096/json/SUB123"
        assert c.build_clash_subscription_url("SUB123") == "https://sub.example:2096/clash/SUB123"
    finally:
        await c.aclose()


async def test_subscription_url_falls_back_to_base_when_unset() -> None:
    c = SanaeiApiClient("https://panel:2053", api_token="t")
    try:
        assert c.build_subscription_url("S1") == "https://panel:2053/sub/S1"
    finally:
        await c.aclose()


async def test_network_error_mapped() -> None:
    def handler(req: httpx.Request) -> httpx.Response:
        raise httpx.ConnectError("boom")

    with pytest.raises(XuiNetworkError):
        async with make_client(handler, api_token="t") as c:
            await c.list_inbounds()


async def test_panel_failure_raises_api_error() -> None:
    def handler(req: httpx.Request) -> httpx.Response:
        return _fail("bad inbound")

    with pytest.raises(XuiApiError):
        async with make_client(handler, api_token="t") as c:
            await c.get_inbound(99)


async def test_secrets_not_logged(caplog) -> None:
    def handler(req: httpx.Request) -> httpx.Response:
        return _ok([])

    with caplog.at_level("DEBUG"):
        async with make_client(handler, api_token="super-secret-token") as c:
            await c.list_inbounds()
    assert "super-secret-token" not in caplog.text


# --------------------------------------------------------------------------
# list_inbounds hardening: endpoint fallback + response-wrapper variants
# --------------------------------------------------------------------------
async def test_list_inbounds_falls_back_to_alt_path_on_404() -> None:
    seen = []

    def handler(req: httpx.Request) -> httpx.Response:
        seen.append(req.url.path)
        if req.url.path.endswith("/panel/api/inbounds/list"):
            return httpx.Response(404, text="not found")
        if req.url.path.endswith("/panel/api/inbounds"):
            return _ok([{"id": 5}])
        return _ok(None)

    async with make_client(handler, api_token="t") as c:
        inbounds = await c.list_inbounds()
    assert inbounds == [{"id": 5}]
    assert any(p.endswith("/panel/api/inbounds/list") for p in seen)  # tried primary first
    assert any(p.endswith("/panel/api/inbounds") for p in seen)       # then the fallback


async def test_list_inbounds_unwraps_nested_list_property() -> None:
    # Some panels answer {success, obj:{inbounds:[...]}} instead of a bare list.
    def handler(req: httpx.Request) -> httpx.Response:
        return _ok({"inbounds": [{"id": 1}, {"id": 2}], "total": 2})

    async with make_client(handler, api_token="t") as c:
        inbounds = await c.list_inbounds()
    assert inbounds == [{"id": 1}, {"id": 2}]


async def test_list_inbounds_unwraps_list_key_and_drops_non_dicts() -> None:
    def handler(req: httpx.Request) -> httpx.Response:
        return _ok({"list": [{"id": 3}, "garbage", None, {"id": 4}]})

    async with make_client(handler, api_token="t") as c:
        inbounds = await c.list_inbounds()
    assert inbounds == [{"id": 3}, {"id": 4}]


async def test_list_inbounds_empty_on_unexpected_shape() -> None:
    def handler(req: httpx.Request) -> httpx.Response:
        return _ok("not a list or dict")

    async with make_client(handler, api_token="t") as c:
        assert await c.list_inbounds() == []

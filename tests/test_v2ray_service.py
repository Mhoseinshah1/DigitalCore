"""Phase 6: real 3X-UI V2Ray provisioning — fully mocked (no real network).

Every panel call goes through an httpx.MockTransport backed by a small stateful
fake panel; the retry backoff uses an injected no-op sleep. Covers provisioning
success + verify-after-write, idempotency/duplicate-prevention, safe failure
paths, and the management operations.
"""
from __future__ import annotations

import json
import urllib.parse

import httpx
import pytest
from sqlalchemy import select

from app.core import crypto
from app.models import AuditLog, Order, Product, User, V2RayService, XuiInbound, XuiServer
from app.services import delivery_service, v2ray_service
from app.services.v2ray_service import V2RayError

PANEL_INBOUND = 55


async def _noop_sleep(_delay: float) -> None:
    return None


def make_panel(state: dict, *, login_ok: bool = True, corrupt_verify: bool = False):
    """A tiny stateful 3X-UI panel. `state['clients']` holds inbound 55's clients.

    `corrupt_verify` makes get-inbound echo a wrong quota so verify-after-write
    fails. `login_ok=False` makes login reject (auth error).
    """
    state.setdefault("clients", [])
    state.setdefault("add_calls", 0)
    state.setdefault("get_calls", 0)

    def handler(request: httpx.Request) -> httpx.Response:
        path = request.url.path
        if path.endswith("/login"):
            return httpx.Response(200, json={"success": bool(login_ok),
                                             "msg": "" if login_ok else "bad creds", "obj": None})
        if f"/panel/api/inbounds/get/{PANEL_INBOUND}" in path:
            state["get_calls"] += 1
            clients = state["clients"]
            if corrupt_verify:
                clients = [{**c, "totalGB": 1} for c in clients]
            inbound = {"id": PANEL_INBOUND, "remark": "in", "protocol": "vless", "port": 443,
                       "enable": True, "settings": json.dumps({"clients": clients})}
            return httpx.Response(200, json={"success": True, "obj": inbound})
        if path.endswith("/panel/api/inbounds/addClient"):
            state["add_calls"] += 1
            body = dict(kv.split("=", 1) for kv in request.content.decode().split("&"))
            settings = json.loads(urllib.parse.unquote_plus(body["settings"]))
            state["clients"].extend(settings["clients"])
            return httpx.Response(200, json={"success": True, "obj": None})
        if "/getClientTraffics/" in path:
            return httpx.Response(200, json={"success": True, "obj": {
                "email": "x", "up": 1500, "down": 2500, "total": 0, "expiryTime": 0}})
        if "/updateClient/" in path:
            body = dict(kv.split("=", 1) for kv in request.content.decode().split("&"))
            newc = json.loads(urllib.parse.unquote_plus(body["settings"]))["clients"][0]
            state["clients"] = [newc if c.get("email") == newc.get("email") else c
                                for c in state["clients"]]
            return httpx.Response(200, json={"success": True, "obj": None})
        if "/delClient/" in path or "/resetClientTraffic/" in path:
            return httpx.Response(200, json={"success": True, "obj": None})
        return httpx.Response(404)

    return httpx.MockTransport(handler)


async def _seed(session, *, server_active=True, inbound_active=True, inbound_server_id=None,
                duration=30, traffic=50, ip_limit=2, sub_host="https://sub.example.com"):
    u = User(telegram_id=555, first_name="B", language="fa")
    srv = XuiServer(name="srv", base_url="http://panel:2053", username="admin",
                    encrypted_password=crypto.encrypt("pw"), panel_version="2.9.4",
                    is_active=server_active, status="online",
                    public_sub_base_url=sub_host, subscription_path="/sub/")
    session.add_all([u, srv])
    await session.flush()
    ib = XuiInbound(server_id=(inbound_server_id or srv.id), inbound_id=PANEL_INBOUND,
                    remark="in", protocol="vless", port=443, is_active=inbound_active)
    session.add(ib)
    await session.flush()
    p = Product(type="v2ray", title="VPN-30", price=1000, duration_days=duration,
                traffic_gb=traffic, ip_limit=ip_limit, is_active=True, is_hidden=False,
                xui_server_id=srv.id, xui_inbound_id=ib.id)
    session.add(p)
    await session.flush()
    o = Order(order_number="DC-20260706-000001", user_id=u.id, product_id=p.id,
              amount=1000, final_amount=1000, status="approved", payment_method="card_to_card")
    session.add(o)
    await session.commit()
    return u, srv, ib, p, o


# --- pure helpers -----------------------------------------------------------
def test_gb_to_bytes() -> None:
    assert v2ray_service.gb_to_bytes(10) == 10 * 1024 ** 3
    assert v2ray_service.gb_to_bytes(0) == 0
    assert v2ray_service.gb_to_bytes(None) == 0


def test_generate_client_email_deterministic() -> None:
    class _O:
        user_id = 7
        order_number = "DC-20260706-000042"
    e1 = v2ray_service.generate_client_email(_O(), None, None)
    e2 = v2ray_service.generate_client_email(_O(), None, None)
    assert e1 == e2 == "dc-u7-odc-20260706-000042"


def test_generate_uuid_unique() -> None:
    assert v2ray_service.generate_client_uuid() != v2ray_service.generate_client_uuid()


def test_calculate_expire_at() -> None:
    class _P:
        duration_days = 30
    exp = v2ray_service.calculate_expire_at(_P())
    assert exp is not None
    class _P0:
        duration_days = 0
    assert v2ray_service.calculate_expire_at(_P0()) is None


# --- provisioning success ---------------------------------------------------
async def test_provision_success(db_session) -> None:
    u, srv, ib, p, o = await _seed(db_session)
    state: dict = {}
    r = await v2ray_service.provision_service_for_order(
        db_session, o.id, transport=make_panel(state), sleep=_noop_sleep)
    assert r["ok"] and r["provisioned"]
    svc = await v2ray_service.get_service_by_order(db_session, o.id)
    order = await db_session.get(Order, o.id)
    assert svc.status == "active" and order.status == "delivered"
    assert svc.total_gb == 50 * 1024 ** 3 and svc.ip_limit == 2
    assert svc.subscription_url and svc.subscription_url.endswith(svc.sub_id)
    assert order.delivered_payload and svc.client_email in order.delivered_payload
    # verify-after-write actually read the client back from the panel.
    assert state["add_calls"] == 1 and state["get_calls"] >= 1


async def test_provision_no_sub_host_leaves_url_null(db_session) -> None:
    u, srv, ib, p, o = await _seed(db_session, sub_host="")
    r = await v2ray_service.provision_service_for_order(
        db_session, o.id, transport=make_panel({}), sleep=_noop_sleep)
    svc = await v2ray_service.get_service_by_order(db_session, o.id)
    assert r["provisioned"] and svc.subscription_url is None and svc.qr_code_path is None


# --- idempotency / duplicate prevention -------------------------------------
async def test_second_provision_returns_same_service(db_session) -> None:
    u, srv, ib, p, o = await _seed(db_session)
    state: dict = {}
    r1 = await v2ray_service.provision_service_for_order(
        db_session, o.id, transport=make_panel(state), sleep=_noop_sleep)
    r2 = await v2ray_service.provision_service_for_order(
        db_session, o.id, transport=make_panel(state), sleep=_noop_sleep)
    assert r1["service_id"] == r2["service_id"] and r2.get("already")
    n = (await db_session.execute(select(V2RayService))).scalars().all()
    assert len(n) == 1


async def test_retry_reuses_deterministic_client_no_duplicate(db_session) -> None:
    """A panel client left by a prior partial run is reused (not duplicated) AND
    the local row adopts the panel client's real uuid/sub_id (crash-repair)."""
    u, srv, ib, p, o = await _seed(db_session)
    # Simulate a prior partial run: the panel already has our deterministic client
    # (with its real uuid + subId), but there is no local service row yet.
    email = v2ray_service.generate_client_email(o, u, p)
    state = {"clients": [{"id": "old-uuid", "email": email, "enable": True,
                          "expiryTime": 0, "totalGB": 50 * 1024 ** 3, "limitIp": 2,
                          "subId": "old-sub-id"}]}
    r = await v2ray_service.provision_service_for_order(
        db_session, o.id, transport=make_panel(state), sleep=_noop_sleep)
    assert r["provisioned"]
    # find_client saw the existing client → NO addClient call, no duplicate.
    assert state["add_calls"] == 0 and len(state["clients"]) == 1
    # The local row adopts the panel client's identity so links/management ops
    # target the client that actually exists on the panel.
    svc = await v2ray_service.get_service_by_order(db_session, o.id)
    assert svc.client_uuid == "old-uuid" and svc.sub_id == "old-sub-id"
    assert svc.subscription_url and svc.subscription_url.endswith("old-sub-id")


# --- safe failure paths -----------------------------------------------------
async def _seed_and_fail(db_session, **seed_kw):
    u, srv, ib, p, o = await _seed(db_session, **seed_kw)
    return o


async def test_fail_server_missing(db_session) -> None:
    u, srv, ib, p, o = await _seed(db_session)
    p.xui_server_id = 99999
    await db_session.commit()
    r = await v2ray_service.provision_service_for_order(
        db_session, o.id, transport=make_panel({}), sleep=_noop_sleep)
    order = await db_session.get(Order, o.id)
    assert r["reason"] == "server_missing" and order.status == "provisioning_pending"
    assert order.delivery_error and order.status != "delivered"


async def test_fail_inactive_server(db_session) -> None:
    u, srv, ib, p, o = await _seed(db_session, server_active=False)
    r = await v2ray_service.provision_service_for_order(
        db_session, o.id, transport=make_panel({}), sleep=_noop_sleep)
    assert r["reason"] == "server_inactive"


async def test_fail_inactive_inbound(db_session) -> None:
    u, srv, ib, p, o = await _seed(db_session, inbound_active=False)
    r = await v2ray_service.provision_service_for_order(
        db_session, o.id, transport=make_panel({}), sleep=_noop_sleep)
    assert r["reason"] == "inbound_inactive"


async def test_fail_inbound_belongs_to_other_server(db_session) -> None:
    # Point the inbound at a different server than the product's.
    u = User(telegram_id=1, first_name="B", language="fa")
    other = XuiServer(name="other", base_url="http://o:2053", username="a",
                      encrypted_password=crypto.encrypt("pw"), panel_version="2.9.4",
                      is_active=True, status="online")
    srv = XuiServer(name="srv", base_url="http://p:2053", username="a",
                    encrypted_password=crypto.encrypt("pw"), panel_version="2.9.4",
                    is_active=True, status="online")
    db_session.add_all([u, other, srv])
    await db_session.flush()
    ib = XuiInbound(server_id=other.id, inbound_id=PANEL_INBOUND, is_active=True)
    db_session.add(ib)
    await db_session.flush()
    p = Product(type="v2ray", title="V", price=1000, duration_days=30, traffic_gb=10,
                ip_limit=1, is_active=True, is_hidden=False,
                xui_server_id=srv.id, xui_inbound_id=ib.id)
    db_session.add(p)
    await db_session.flush()
    o = Order(order_number="DC-X-9", user_id=u.id, product_id=p.id, amount=1000,
              final_amount=1000, status="approved", payment_method="card_to_card")
    db_session.add(o)
    await db_session.commit()
    r = await v2ray_service.provision_service_for_order(
        db_session, o.id, transport=make_panel({}), sleep=_noop_sleep)
    assert r["reason"] == "inbound_mismatch"


async def test_fail_auth_error_marks_failed(db_session) -> None:
    u, srv, ib, p, o = await _seed(db_session)
    r = await v2ray_service.provision_service_for_order(
        db_session, o.id, transport=make_panel({}, login_ok=False), sleep=_noop_sleep)
    assert r["reason"] == "panel_error"
    svc = await v2ray_service.get_service_by_order(db_session, o.id)
    order = await db_session.get(Order, o.id)
    assert svc.status == "failed" and svc.last_error
    assert order.status == "provisioning_pending" and order.status != "delivered"


async def test_fail_verify_mismatch(db_session) -> None:
    u, srv, ib, p, o = await _seed(db_session)
    r = await v2ray_service.provision_service_for_order(
        db_session, o.id, transport=make_panel({}, corrupt_verify=True), sleep=_noop_sleep)
    assert r["reason"] == "verify_failed"
    svc = await v2ray_service.get_service_by_order(db_session, o.id)
    assert svc.status == "failed"


async def test_retry_after_failure_succeeds(db_session) -> None:
    u, srv, ib, p, o = await _seed(db_session)
    r1 = await v2ray_service.provision_service_for_order(
        db_session, o.id, transport=make_panel({}, login_ok=False), sleep=_noop_sleep)
    assert not r1["ok"]
    state: dict = {}
    r2 = await v2ray_service.retry_failed_provisioning(
        db_session, o.id, transport=make_panel(state), sleep=_noop_sleep)
    assert r2["ok"] and r2["provisioned"]
    svc = await v2ray_service.get_service_by_order(db_session, o.id)
    assert svc.status == "active"
    n = (await db_session.execute(select(V2RayService))).scalars().all()
    assert len(n) == 1 and state["add_calls"] == 1


# --- notification failure does not create a duplicate -----------------------
class _FailBot:
    async def send_message(self, *a, **k):
        raise RuntimeError("telegram down")

    async def send_photo(self, *a, **k):
        raise RuntimeError("telegram down")


async def test_notify_failure_does_not_duplicate(db_session) -> None:
    u, srv, ib, p, o = await _seed(db_session)
    state: dict = {}
    r = await v2ray_service.provision_service_for_order(
        db_session, o.id, bot=_FailBot(), transport=make_panel(state), sleep=_noop_sleep)
    # Provisioning succeeded even though the Telegram send raised.
    assert r["provisioned"] and state["add_calls"] == 1
    svc = await v2ray_service.get_service_by_order(db_session, o.id)
    assert svc.status == "active"


# --- dispatcher -------------------------------------------------------------
async def test_dispatcher_v2ray_provisions(db_session, monkeypatch) -> None:
    u, srv, ib, p, o = await _seed(db_session)
    state: dict = {}
    # deliver_order does not thread transport, so wrap the real provisioner to
    # inject the mock panel. Capture the original to avoid recursing into _prov.
    orig = v2ray_service.provision_service_for_order
    async def _prov(session, order_id, **kw):
        return await orig(session, order_id, transport=make_panel(state), sleep=_noop_sleep, **kw)
    monkeypatch.setattr(v2ray_service, "provision_service_for_order", _prov)
    order = await db_session.get(Order, o.id)
    result = await delivery_service.deliver_order(db_session, order)
    assert result["delivered"] is True and result.get("service_id")


# --- management operations --------------------------------------------------
async def _provisioned(db_session):
    u, srv, ib, p, o = await _seed(db_session)
    await v2ray_service.provision_service_for_order(
        db_session, o.id, transport=make_panel({}), sleep=_noop_sleep)
    return await v2ray_service.get_service_by_order(db_session, o.id)


async def test_refresh_usage(db_session) -> None:
    svc = await _provisioned(db_session)
    updated = await v2ray_service.refresh_service_usage(
        db_session, svc.id, transport=make_panel({}), sleep=_noop_sleep)
    assert updated.used_gb == 1500 + 2500 and updated.last_traffic_sync_at is not None


async def test_disable_enable_delete_reset(db_session) -> None:
    svc = await _provisioned(db_session)
    d = await v2ray_service.disable_service(db_session, svc.id, transport=make_panel({}), sleep=_noop_sleep)
    assert d.status == "disabled" and d.disabled_at is not None
    e = await v2ray_service.enable_service(db_session, svc.id, transport=make_panel({}), sleep=_noop_sleep)
    assert e.status == "active"
    rst = await v2ray_service.reset_service_traffic(db_session, svc.id, transport=make_panel({}), sleep=_noop_sleep)
    assert rst.used_gb == 0
    dl = await v2ray_service.delete_service(db_session, svc.id, transport=make_panel({}), sleep=_noop_sleep)
    assert dl.status == "deleted" and dl.deleted_at is not None


async def test_mark_expired_services(db_session) -> None:
    from datetime import datetime, timedelta, timezone
    svc = await _provisioned(db_session)
    svc.expire_at = datetime.now(timezone.utc) - timedelta(days=1)
    await db_session.commit()
    n = await v2ray_service.mark_expired_services(db_session)
    refreshed = await v2ray_service.get_service(db_session, svc.id)
    assert n == 1 and refreshed.status == "expired"


# --- security ---------------------------------------------------------------
async def test_audit_and_service_never_leak_panel_password(db_session) -> None:
    u, srv, ib, p, o = await _seed(db_session)
    await v2ray_service.provision_service_for_order(
        db_session, o.id, transport=make_panel({}), sleep=_noop_sleep)
    rows = (await db_session.execute(select(AuditLog))).scalars().all()
    blob = " ".join(f"{r.old_value} {r.new_value} {r.meta}" for r in rows)
    assert "pw" not in blob.split()  # the panel password never appears as a token
    assert "enc::" not in blob       # nor the ciphertext

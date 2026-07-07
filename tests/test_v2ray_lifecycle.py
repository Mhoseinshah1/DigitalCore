"""Phase 8: V2Ray lifecycle — renewal, add-traffic, sweeps, and order-driven
service actions. Panel calls go through the same tiny stateful mock as Phase 6;
DB-only sweeps need no panel. No real network, no real credentials.
"""
from __future__ import annotations

import json
import urllib.parse
from datetime import datetime, timedelta, timezone

import httpx

from app.core import crypto
from app.models import Order, Product, User, V2RayService, XuiInbound, XuiServer
from app.services import (
    delivery_service,
    order_service,
    product_service,
    v2ray_lifecycle_service,
    v2ray_service,
)
from app.services.order_service import OrderError

PANEL_INBOUND = 55
GB = 1024 ** 3


async def _noop_sleep(_delay: float) -> None:
    return None


def _aw(dt: datetime | None) -> datetime | None:
    """Treat a naive datetime (read back from SQLite) as UTC for comparisons."""
    if dt is None or dt.tzinfo is not None:
        return dt
    return dt.replace(tzinfo=timezone.utc)


def make_panel(state: dict, *, login_ok: bool = True):
    """A tiny stateful 3X-UI panel (subset of the Phase 6 test panel)."""
    state.setdefault("clients", [])

    def handler(request: httpx.Request) -> httpx.Response:
        path = request.url.path
        if path.endswith("/login"):
            return httpx.Response(200, json={"success": bool(login_ok),
                                             "msg": "" if login_ok else "bad", "obj": None})
        if f"/panel/api/inbounds/get/{PANEL_INBOUND}" in path:
            inbound = {"id": PANEL_INBOUND, "remark": "in", "protocol": "vless", "port": 443,
                       "enable": True, "settings": json.dumps({"clients": state["clients"]})}
            return httpx.Response(200, json={"success": True, "obj": inbound})
        if path.endswith("/panel/api/inbounds/addClient"):
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


async def _seed(session, *, duration=30, traffic=50):
    u = User(telegram_id=555, first_name="B", language="fa")
    srv = XuiServer(name="srv", base_url="http://panel:2053", username="admin",
                    encrypted_password=crypto.encrypt("pw"), panel_version="2.9.4",
                    is_active=True, status="online",
                    public_sub_base_url="https://sub.example.com", subscription_path="/sub/")
    session.add_all([u, srv])
    await session.flush()
    ib = XuiInbound(server_id=srv.id, inbound_id=PANEL_INBOUND, remark="in", protocol="vless",
                    port=443, is_active=True)
    session.add(ib)
    await session.flush()
    p = Product(type="v2ray", title="VPN-30", price=1000, duration_days=duration,
                traffic_gb=traffic, ip_limit=2, is_active=True, is_hidden=False,
                xui_server_id=srv.id, xui_inbound_id=ib.id)
    session.add(p)
    await session.flush()
    o = Order(order_number="DC-20260706-000001", user_id=u.id, product_id=p.id,
              amount=1000, final_amount=1000, status="approved", payment_method="card_to_card")
    session.add(o)
    await session.commit()
    return u, srv, ib, p, o


async def _provisioned(session, state):
    u, srv, ib, p, o = await _seed(session)
    await v2ray_service.provision_service_for_order(
        session, o.id, transport=make_panel(state), sleep=_noop_sleep)
    svc = await v2ray_service.get_service_by_order(session, o.id)
    return u, srv, ib, p, o, svc


async def _action_product(session, action_type, *, duration=0, traffic=0, price=500):
    p = Product(type="v2ray", title=f"plan-{action_type}", price=price,
                duration_days=duration or None, traffic_gb=traffic or None,
                ip_limit=1, is_active=True, is_hidden=False,
                action_type=action_type, applies_to_service=True)
    session.add(p)
    await session.commit()
    return p


# --- pure helpers -----------------------------------------------------------
async def test_renewed_expiry_extends_from_future(db_session) -> None:
    _, _, _, _, _, svc = await _provisioned(db_session, {})
    original = _aw(svc.expire_at)
    new = v2ray_lifecycle_service._renewed_expiry(svc, 10)
    # base is the current (future) expiry, +10 days
    assert new > original
    assert abs((new - original).days - 10) <= 1


async def test_renewed_expiry_from_now_when_expired(db_session) -> None:
    _, _, _, _, _, svc = await _provisioned(db_session, {})
    svc.expire_at = datetime.now(timezone.utc) - timedelta(days=5)
    new = v2ray_lifecycle_service._renewed_expiry(svc, 7)
    # past expiry is ignored: base is now, so ~7 days out (not 2)
    assert new > datetime.now(timezone.utc) + timedelta(days=6)


async def test_added_total_keeps_unlimited(db_session) -> None:
    _, _, _, _, _, svc = await _provisioned(db_session, {})
    svc.total_gb = 0  # unlimited
    assert v2ray_lifecycle_service._added_total(svc, 100) == 0


# --- renew / add-traffic (admin, direct) ------------------------------------
async def test_renew_service_extends_and_reenables(db_session) -> None:
    state: dict = {}
    _, _, _, _, _, svc = await _provisioned(db_session, state)
    svc.status = "expired"
    svc.expire_at = datetime.now(timezone.utc) - timedelta(days=1)
    svc.last_expiry_warning_at = datetime.now(timezone.utc)
    await db_session.commit()

    updated = await v2ray_lifecycle_service.renew_service(
        db_session, svc.id, duration_days=30, transport=make_panel(state), sleep=_noop_sleep)
    await db_session.commit()
    assert updated.status == "active"
    assert updated.expire_at > datetime.now(timezone.utc) + timedelta(days=29)
    assert updated.last_expiry_warning_at is None  # reset for the fresh window


async def test_add_traffic_increases_total_and_clears_over_quota(db_session) -> None:
    state: dict = {}
    _, _, _, _, _, svc = await _provisioned(db_session, state)
    svc.status = "over_quota"
    svc.over_quota_at = datetime.now(timezone.utc)
    svc.used_gb = svc.total_gb  # exactly at quota
    before = int(svc.total_gb)
    await db_session.commit()

    updated = await v2ray_lifecycle_service.add_traffic(
        db_session, svc.id, traffic_gb=10, transport=make_panel(state), sleep=_noop_sleep)
    await db_session.commit()
    assert updated.total_gb == before + 10 * GB
    assert updated.status == "active"  # used < new total
    assert updated.over_quota_at is None


async def test_renew_panel_failure_raises_and_leaves_service(db_session) -> None:
    state: dict = {}
    _, _, _, _, _, svc = await _provisioned(db_session, state)
    original_expiry = svc.expire_at
    try:
        await v2ray_lifecycle_service.renew_service(
            db_session, svc.id, duration_days=30,
            transport=make_panel(state, login_ok=False), sleep=_noop_sleep)
        assert False, "expected V2RayError"
    except v2ray_service.V2RayError:
        pass
    # The local service is untouched because we only assign after a verified write.
    assert svc.expire_at == original_expiry


# --- usage refresh + over-quota detection -----------------------------------
async def test_refresh_detects_over_quota(db_session) -> None:
    state: dict = {}
    _, _, _, _, _, svc = await _provisioned(db_session, state)
    svc.total_gb = 1000  # tiny quota; mock reports used=4000 bytes
    await db_session.commit()
    updated = await v2ray_lifecycle_service.refresh_usage(
        db_session, svc.id, transport=make_panel(state), sleep=_noop_sleep)
    assert updated.used_gb == 4000
    assert updated.status == "over_quota" and updated.over_quota_at is not None


# --- DB-only sweeps ---------------------------------------------------------
async def test_mark_over_quota_services(db_session) -> None:
    _, _, _, _, _, svc = await _provisioned(db_session, {})
    svc.total_gb = 100
    svc.used_gb = 100
    await db_session.commit()
    n = await v2ray_lifecycle_service.mark_over_quota_services(db_session)
    refreshed = await v2ray_service.get_service(db_session, svc.id)
    assert n == 1 and refreshed.status == "over_quota" and refreshed.over_quota_at is not None


async def test_mark_expired_stamps_expired_at(db_session) -> None:
    _, _, _, _, _, svc = await _provisioned(db_session, {})
    svc.expire_at = datetime.now(timezone.utc) - timedelta(days=1)
    await db_session.commit()
    n = await v2ray_lifecycle_service.mark_expired_services(db_session)
    refreshed = await v2ray_service.get_service(db_session, svc.id)
    assert n == 1 and refreshed.status == "expired" and refreshed.expired_at is not None


# --- auto-disable (gated + idempotent) --------------------------------------
async def test_disable_over_quota_gated_and_idempotent(db_session) -> None:
    state: dict = {}
    _, _, _, _, _, svc = await _provisioned(db_session, state)
    svc.status = "over_quota"
    await db_session.commit()

    n = await v2ray_lifecycle_service.disable_over_quota_services(
        db_session, transport=make_panel(state), sleep=_noop_sleep)
    assert n == 1
    refreshed = await v2ray_service.get_service(db_session, svc.id)
    assert refreshed.disabled_at is not None
    # Second pass is a no-op (disabled_at guard), so it never re-hits the panel.
    n2 = await v2ray_lifecycle_service.disable_over_quota_services(
        db_session, transport=make_panel(state), sleep=_noop_sleep)
    assert n2 == 0


async def test_disable_expired_respects_setting_off(db_session) -> None:
    from app.core.settings_service import SettingsService
    state: dict = {}
    _, _, _, _, _, svc = await _provisioned(db_session, state)
    svc.status = "expired"
    await db_session.commit()
    await SettingsService(db_session).set("v2ray_auto_disable_expired", False)
    n = await v2ray_lifecycle_service.disable_expired_services(
        db_session, transport=make_panel(state), sleep=_noop_sleep)
    assert n == 0


# --- one-shot warnings ------------------------------------------------------
async def test_expiry_warning_fires_once(db_session) -> None:
    _, _, _, _, _, svc = await _provisioned(db_session, {})
    svc.expire_at = datetime.now(timezone.utc) + timedelta(days=2)  # inside default 3-day window
    await db_session.commit()
    n = await v2ray_lifecycle_service.send_expiry_warnings(db_session)
    assert n == 1
    refreshed = await v2ray_service.get_service(db_session, svc.id)
    assert refreshed.last_expiry_warning_at is not None
    # A second sweep does not warn again (guarded by the stamp).
    assert await v2ray_lifecycle_service.send_expiry_warnings(db_session) == 0


async def test_traffic_warning_fires_once(db_session) -> None:
    _, _, _, _, _, svc = await _provisioned(db_session, {})
    svc.total_gb = 100
    svc.used_gb = 95  # 95% >= default 90%, still under quota
    await db_session.commit()
    n = await v2ray_lifecycle_service.send_traffic_warnings(db_session)
    assert n == 1
    refreshed = await v2ray_service.get_service(db_session, svc.id)
    assert refreshed.last_traffic_warning_at is not None
    assert await v2ray_lifecycle_service.send_traffic_warnings(db_session) == 0


# --- order-driven service actions -------------------------------------------
async def _renew_order(session, user, service, product):
    o = Order(order_number="DC-20260706-000002", user_id=user.id, product_id=product.id,
              amount=500, final_amount=500, status="approved", payment_method="card_to_card",
              action_type="renew_service", target_service_id=service.id)
    session.add(o)
    await session.commit()
    return o


async def test_apply_action_renew_delivers_and_is_idempotent(db_session) -> None:
    state: dict = {}
    u, _, _, _, _, svc = await _provisioned(db_session, state)
    rp = await _action_product(db_session, "renew_service", duration=30)
    order = await _renew_order(db_session, u, svc, rp)
    before = _aw(svc.expire_at)

    result = await v2ray_lifecycle_service.apply_service_action_for_order(
        db_session, order.id, transport=make_panel(state), sleep=_noop_sleep)
    assert result["ok"] and result.get("provisioned")
    o = await order_service.get_order(db_session, order.id)
    refreshed = await v2ray_service.get_service(db_session, svc.id)
    assert o.status == "delivered"
    assert _aw(refreshed.expire_at) > before

    # Redelivery is a no-op (order already delivered).
    again = await v2ray_lifecycle_service.apply_service_action_for_order(
        db_session, order.id, transport=make_panel(state), sleep=_noop_sleep)
    assert again.get("already") is True


async def test_apply_action_panel_failure_keeps_order_retryable(db_session) -> None:
    state: dict = {}
    u, _, _, _, _, svc = await _provisioned(db_session, state)
    before = svc.expire_at
    rp = await _action_product(db_session, "renew_service", duration=30)
    order = await _renew_order(db_session, u, svc, rp)

    result = await v2ray_lifecycle_service.apply_service_action_for_order(
        db_session, order.id, transport=make_panel(state, login_ok=False), sleep=_noop_sleep)
    assert result["ok"] is False
    o = await order_service.get_order(db_session, order.id)
    refreshed = await v2ray_service.get_service(db_session, svc.id)
    # Payment/approval is untouched; the order is retryable and the service intact.
    assert o.status == "provisioning_pending" and o.delivery_error
    assert refreshed.expire_at == before


async def test_apply_action_wrong_owner_fails(db_session) -> None:
    state: dict = {}
    u, _, _, _, _, svc = await _provisioned(db_session, state)
    other = User(telegram_id=999, first_name="Other")
    db_session.add(other)
    await db_session.flush()
    rp = await _action_product(db_session, "renew_service", duration=30)
    # An order whose buyer is NOT the service owner.
    o = Order(order_number="DC-20260706-000003", user_id=other.id, product_id=rp.id,
              amount=500, final_amount=500, status="approved", payment_method="card_to_card",
              action_type="renew_service", target_service_id=svc.id)
    db_session.add(o)
    await db_session.commit()
    result = await v2ray_lifecycle_service.apply_service_action_for_order(
        db_session, o.id, transport=make_panel(state), sleep=_noop_sleep)
    assert result["ok"] is False and result["reason"] == "not_owner"


async def test_delivery_routes_action_order(db_session) -> None:
    state: dict = {}
    u, _, _, _, _, svc = await _provisioned(db_session, state)
    rp = await _action_product(db_session, "add_traffic", traffic=20)
    o = Order(order_number="DC-20260706-000004", user_id=u.id, product_id=rp.id,
              amount=500, final_amount=500, status="approved", payment_method="card_to_card",
              action_type="add_traffic", target_service_id=svc.id)
    db_session.add(o)
    await db_session.commit()
    before = int(svc.total_gb)

    # Patch the lifecycle apply to inject the mock transport (deliver_order does
    # not thread transport), mirroring the Phase 6 dispatcher test.
    orig = v2ray_lifecycle_service.apply_service_action_for_order

    async def _apply(session, order_id, **kw):
        return await orig(session, order_id, transport=make_panel(state), sleep=_noop_sleep, **kw)

    v2ray_lifecycle_service.apply_service_action_for_order = _apply
    try:
        order = await order_service.get_order(db_session, o.id)
        result = await delivery_service.deliver_order(db_session, order)
    finally:
        v2ray_lifecycle_service.apply_service_action_for_order = orig
    assert result["delivered"] is True
    refreshed = await v2ray_service.get_service(db_session, svc.id)
    assert refreshed.total_gb == before + 20 * GB


# --- order_service validation for action orders -----------------------------
async def test_create_order_action_requires_ownership(db_session) -> None:
    state: dict = {}
    u, _, _, _, _, svc = await _provisioned(db_session, state)
    other = User(telegram_id=1001, first_name="X")
    db_session.add(other)
    await db_session.flush()
    rp = await _action_product(db_session, "renew_service", duration=30)
    # `other` cannot renew a service they do not own.
    try:
        await order_service.create_order(
            db_session, other.id, rp.id,
            action_type="renew_service", target_service_id=svc.id)
        assert False, "expected OrderError"
    except OrderError as exc:
        assert exc.code == "not_your_service"


async def test_create_order_action_product_mismatch(db_session) -> None:
    state: dict = {}
    u, _, _, _, _, svc = await _provisioned(db_session, state)
    # An add-traffic product used with a renew action must be refused.
    ap = await _action_product(db_session, "add_traffic", traffic=20)
    try:
        await order_service.create_order(
            db_session, u.id, ap.id,
            action_type="renew_service", target_service_id=svc.id)
        assert False, "expected OrderError"
    except OrderError as exc:
        assert exc.code == "product_action_mismatch"


async def test_action_product_cannot_be_bought_standalone(db_session) -> None:
    u, _, _, _, _, svc = await _provisioned(db_session, {})
    rp = await _action_product(db_session, "renew_service", duration=30)
    try:
        await order_service.create_order(db_session, u.id, rp.id)
        assert False, "expected OrderError"
    except OrderError as exc:
        assert exc.code == "requires_service"


async def test_action_products_excluded_from_catalog(db_session) -> None:
    await _provisioned(db_session, {})
    await _action_product(db_session, "renew_service", duration=30)
    catalog = await product_service.list_for_user(db_session)
    # The normal catalog shows the base product but not the renew product.
    titles = {p.title for p in catalog}
    assert "VPN-30" in titles
    assert not any(p.applies_to_service for p in catalog)
    plans = await product_service.list_service_action_products(db_session, "renew_service")
    assert len(plans) == 1 and plans[0].action_type == "renew_service"

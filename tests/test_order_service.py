"""order_service: creation rules, numbering, listing, cancellation, audit."""
from __future__ import annotations

import pytest
from sqlalchemy import select

from app.models import AuditLog, Product, Setting, User
from app.services import order_service
from app.services.order_service import OrderError


async def _user(db_session, tg=100) -> User:
    u = User(telegram_id=tg, first_name="Buyer")
    db_session.add(u)
    await db_session.flush()
    return u


async def _license(db_session, **kw) -> Product:
    payload = dict(type="license", title="Key", price=120_000, is_active=True, is_hidden=False)
    payload.update(kw)
    p = Product(**payload)
    db_session.add(p)
    await db_session.flush()
    return p


async def _set(db_session, key, value) -> None:
    db_session.add(Setting(key=key, value=value))
    await db_session.flush()


async def test_create_order_ok_and_number_format(db_session) -> None:
    u = await _user(db_session)
    p = await _license(db_session)
    order = await order_service.create_order(db_session, u.id, p.id)
    await db_session.commit()
    assert order.status == "pending_payment"
    assert order.payment_method == "card_to_card"
    assert order.amount == 120_000
    assert order.discount_amount == 0
    assert order.final_amount == 120_000  # price - discount
    assert order.order_number.startswith("DC-") and order.order_number.count("-") == 2


async def test_order_numbers_are_unique(db_session) -> None:
    u = await _user(db_session)
    p = await _license(db_session)
    a = await order_service.create_order(db_session, u.id, p.id)
    b = await order_service.create_order(db_session, u.id, p.id)
    await db_session.commit()
    assert a.order_number != b.order_number


async def test_cannot_order_inactive_product(db_session) -> None:
    u = await _user(db_session)
    p = await _license(db_session, is_active=False)
    with pytest.raises(OrderError) as ei:
        await order_service.create_order(db_session, u.id, p.id)
    assert ei.value.code == "product_unavailable"


async def test_cannot_order_hidden_product(db_session) -> None:
    u = await _user(db_session)
    p = await _license(db_session, is_hidden=True)
    with pytest.raises(OrderError) as ei:
        await order_service.create_order(db_session, u.id, p.id)
    assert ei.value.code == "product_unavailable"


async def test_cannot_order_when_sales_disabled(db_session) -> None:
    u = await _user(db_session)
    p = await _license(db_session)
    await _set(db_session, "sales_enabled", "false")
    with pytest.raises(OrderError) as ei:
        await order_service.create_order(db_session, u.id, p.id)
    assert ei.value.code == "sales_disabled"


async def test_cannot_order_when_card_disabled(db_session) -> None:
    u = await _user(db_session)
    p = await _license(db_session)
    await _set(db_session, "card_to_card_enabled", "false")
    with pytest.raises(OrderError) as ei:
        await order_service.create_order(db_session, u.id, p.id)
    assert ei.value.code == "card_disabled"


async def test_cannot_order_zero_price(db_session) -> None:
    u = await _user(db_session)
    p = await _license(db_session, price=0)
    with pytest.raises(OrderError) as ei:
        await order_service.create_order(db_session, u.id, p.id)
    assert ei.value.code == "invalid_price"


async def test_cannot_order_v2ray_without_binding(db_session) -> None:
    u = await _user(db_session)
    # A misconfigured v2ray product inserted directly (bypassing product_service).
    p = Product(type="v2ray", title="VPN", price=90_000, duration_days=30,
                traffic_gb=50, is_active=True, is_hidden=False,
                xui_server_id=None, xui_inbound_id=None)
    db_session.add(p)
    await db_session.flush()
    with pytest.raises(OrderError) as ei:
        await order_service.create_order(db_session, u.id, p.id)
    assert ei.value.code == "product_misconfigured"


async def _bound_v2ray(db_session, *, server_active=True, inbound_active=True, bind=True):
    """A v2ray product bound to a server + inbound (Phase 2: auto-synced)."""
    from app.models import XuiInbound, XuiServer
    srv = XuiServer(name="Germany", base_url="http://p", is_active=server_active,
                    status="active")
    db_session.add(srv)
    await db_session.flush()
    inb = XuiInbound(server_id=srv.id, inbound_id=7, remark="R", protocol="vless",
                     is_active=inbound_active)
    db_session.add(inb)
    await db_session.flush()
    p = Product(type="v2ray", title="VPN", price=90_000, duration_days=30,
                traffic_gb=50, is_active=True, is_hidden=False,
                xui_server_id=srv.id if bind else None,
                xui_inbound_id=inb.id if bind else None)
    db_session.add(p)
    await db_session.flush()
    return p, srv, inb


async def test_can_order_v2ray_with_active_binding(db_session) -> None:
    u = await _user(db_session)
    p, _srv, _inb = await _bound_v2ray(db_session)
    order = await order_service.create_order(db_session, u.id, p.id)
    await db_session.commit()
    assert order.status == "pending_payment" and order.amount == 90_000


async def test_cannot_order_v2ray_with_inactive_inbound(db_session) -> None:
    # Inbound present but disabled locally (removed from sale) → never charge.
    u = await _user(db_session)
    p, _srv, _inb = await _bound_v2ray(db_session, inbound_active=False)
    with pytest.raises(OrderError) as ei:
        await order_service.create_order(db_session, u.id, p.id)
    assert ei.value.code == "product_inbound_invalid"


async def test_cannot_order_v2ray_with_inactive_server(db_session) -> None:
    u = await _user(db_session)
    p, _srv, _inb = await _bound_v2ray(db_session, server_active=False)
    with pytest.raises(OrderError) as ei:
        await order_service.create_order(db_session, u.id, p.id)
    assert ei.value.code == "product_inbound_invalid"


async def test_cannot_order_v2ray_with_deleted_inbound(db_session) -> None:
    u = await _user(db_session)
    p, _srv, inb = await _bound_v2ray(db_session)
    await db_session.delete(inb)
    await db_session.flush()
    with pytest.raises(OrderError) as ei:
        await order_service.create_order(db_session, u.id, p.id)
    assert ei.value.code == "product_inbound_invalid"


async def test_supported_payment_methods(db_session) -> None:
    u = await _user(db_session)
    p = await _license(db_session)
    # Phase 7: wallet is now a valid payment method.
    order = await order_service.create_order(db_session, u.id, p.id, payment_method="wallet")
    assert order.payment_method == "wallet"
    # An unimplemented method is still refused.
    with pytest.raises(OrderError) as ei:
        await order_service.create_order(db_session, u.id, p.id, payment_method="gateway")
    assert ei.value.code == "method_unsupported"


async def test_list_user_orders(db_session) -> None:
    u = await _user(db_session)
    p = await _license(db_session)
    await order_service.create_order(db_session, u.id, p.id)
    await order_service.create_order(db_session, u.id, p.id)
    await db_session.commit()
    orders = await order_service.list_user_orders(db_session, u.id)
    assert len(orders) == 2
    # Most-recent first.
    assert orders[0].id > orders[1].id


async def test_cancel_order(db_session) -> None:
    u = await _user(db_session)
    p = await _license(db_session)
    order = await order_service.create_order(db_session, u.id, p.id)
    await db_session.commit()
    cancelled = await order_service.cancel_order(db_session, order.id, user_id=u.id)
    await db_session.commit()
    assert cancelled.status == "cancelled" and cancelled.cancelled_at is not None
    # Wrong owner cannot cancel.
    order2 = await order_service.create_order(db_session, u.id, p.id)
    await db_session.commit()
    with pytest.raises(OrderError) as ei:
        await order_service.cancel_order(db_session, order2.id, user_id=99999)
    assert ei.value.code == "not_your_order"


async def test_order_creation_writes_audit(db_session) -> None:
    u = await _user(db_session)
    p = await _license(db_session)
    order = await order_service.create_order(db_session, u.id, p.id)
    await db_session.commit()
    actions = [
        r.action for r in (await db_session.execute(select(AuditLog))).scalars().all()
        if r.target_type == "order" and str(r.target_id) == str(order.id)
    ]
    assert "order_created" in actions

"""Phase 4 services: wallet adjustments, restriction, license pool, approve/reject,
delivery, and audit logging."""
from __future__ import annotations

import pytest
from sqlalchemy import select

from app.models import AuditLog, Product, Setting, User, WalletTransaction
from app.services import (
    license_service,
    order_service,
    payment_service,
    user_service,
    wallet_service,
)
from app.services.payment_service import ReceiptError, ReceiptFile


async def _user(db_session, tg=100) -> User:
    u = User(telegram_id=tg, first_name="Buyer", wallet_balance=0)
    db_session.add(u)
    await db_session.flush()
    return u


async def _license_product(db_session) -> Product:
    p = Product(type="license", title="Key", price=50_000, is_active=True, is_hidden=False)
    db_session.add(p)
    await db_session.flush()
    return p


async def _submitted_order(db_session, user, product, tmp_path):
    payment_service.RECEIPTS_ROOT = tmp_path
    order = await order_service.create_order(db_session, user.id, product.id)
    await payment_service.create_payment_for_order(db_session, order)
    fi = ReceiptFile(content=b"\x89PNG\r\n\x1a\n" + b"x" * 20,
                     original_name="r.png", mime_type="image/png", file_id="f")
    await payment_service.submit_receipt(db_session, order.id, user.id, fi)
    return order


# --- wallet ------------------------------------------------------------------
async def test_wallet_add_and_subtract_transactional(db_session) -> None:
    u = await _user(db_session)
    await wallet_service.add_balance(db_session, u.id, 10_000, admin_id=9, reason="gift")
    await wallet_service.subtract_balance(db_session, u.id, 3_000, admin_id=9, reason="fix")
    await db_session.commit()
    u2 = await user_service.get_by_id(db_session, u.id)
    assert u2.wallet_balance == 7_000
    txns = (await db_session.execute(
        select(WalletTransaction).order_by(WalletTransaction.id)
    )).scalars().all()
    assert [t.amount for t in txns] == [10_000, -3_000]
    assert txns[0].balance_before == 0 and txns[0].balance_after == 10_000
    assert txns[1].balance_before == 10_000 and txns[1].balance_after == 7_000
    assert all(t.type == "admin_adjustment" and t.actor_id == 9 for t in txns)


async def test_wallet_reason_required(db_session) -> None:
    u = await _user(db_session)
    with pytest.raises(ValueError):
        await wallet_service.add_balance(db_session, u.id, 100, admin_id=9, reason="  ")


async def test_wallet_negative_guard_and_override(db_session) -> None:
    u = await _user(db_session)
    with pytest.raises(ValueError):
        await wallet_service.subtract_balance(db_session, u.id, 5_000, admin_id=9, reason="x")
    # With allow_negative_wallet on, the debit is permitted.
    db_session.add(Setting(key="allow_negative_wallet", value="true"))
    await db_session.flush()
    await wallet_service.subtract_balance(db_session, u.id, 5_000, admin_id=9, reason="x")
    await db_session.commit()
    u2 = await user_service.get_by_id(db_session, u.id)
    assert u2.wallet_balance == -5_000


# --- restriction -------------------------------------------------------------
async def test_set_restricted_updates_fields_and_audit(db_session) -> None:
    u = await _user(db_session)
    await user_service.set_restricted(db_session, u.id, True, reason="risk", actor_id=9)
    await db_session.commit()
    u2 = await user_service.get_by_id(db_session, u.id)
    assert u2.is_restricted and u2.restriction_reason == "risk"
    await user_service.set_restricted(db_session, u.id, False, actor_id=9)
    await db_session.commit()
    u3 = await user_service.get_by_id(db_session, u.id)
    assert not u3.is_restricted and u3.restriction_reason is None
    actions = [r.action for r in (await db_session.execute(select(AuditLog))).scalars().all()]
    assert "user.restricted" in actions and "user.unrestricted" in actions


# --- license pool ------------------------------------------------------------
async def test_license_pool_add_and_assign(db_session) -> None:
    p = await _license_product(db_session)
    added = await license_service.add_keys(db_session, p.id, ["A", "B", "A"], actor_id=9)
    assert added == 2  # duplicate "A" skipped
    assert await license_service.available_count(db_session, p.id) == 2
    key = await license_service.assign_next(db_session, p.id, order_id=123)
    assert key.is_used and key.order_id == 123
    assert await license_service.available_count(db_session, p.id) == 1


# --- approve / reject / delivery --------------------------------------------
async def test_approve_delivers_license(db_session, tmp_path) -> None:
    u = await _user(db_session)
    p = await _license_product(db_session)
    await license_service.add_keys(db_session, p.id, ["LIC-1"], actor_id=9)
    order = await _submitted_order(db_session, u, p, tmp_path)
    await db_session.commit()

    result = await payment_service.approve_payment(db_session, order.id, admin_id=None)
    await db_session.commit()
    o = await order_service.get_order(db_session, order.id)
    assert o.status == "delivered" and o.delivered_payload == "LIC-1"
    assert result["delivery"]["delivered"] is True
    actions = [r.action for r in (await db_session.execute(select(AuditLog))).scalars().all()]
    assert "payment_approved" in actions and "order_delivered" in actions


async def test_approve_without_keys_stays_approved(db_session, tmp_path) -> None:
    u = await _user(db_session)
    p = await _license_product(db_session)  # no keys stocked
    order = await _submitted_order(db_session, u, p, tmp_path)
    await db_session.commit()
    result = await payment_service.approve_payment(db_session, order.id, admin_id=None)
    await db_session.commit()
    o = await order_service.get_order(db_session, order.id)
    assert o.status == "approved" and o.delivered_payload is None
    assert result["delivery"]["reason"] == "no_license_keys"


async def test_reject_sets_reason(db_session, tmp_path) -> None:
    u = await _user(db_session)
    p = await _license_product(db_session)
    order = await _submitted_order(db_session, u, p, tmp_path)
    await db_session.commit()
    await payment_service.reject_payment(db_session, order.id, admin_id=None, reason="blurry")
    await db_session.commit()
    o = await order_service.get_order(db_session, order.id)
    assert o.status == "rejected" and o.reject_reason == "blurry"


async def test_reject_requires_reason(db_session, tmp_path) -> None:
    u = await _user(db_session)
    p = await _license_product(db_session)
    order = await _submitted_order(db_session, u, p, tmp_path)
    await db_session.commit()
    with pytest.raises(ReceiptError) as ei:
        await payment_service.reject_payment(db_session, order.id, admin_id=None, reason="")
    assert ei.value.code == "reason_required"


async def test_duplicate_approve_and_reject_blocked(db_session, tmp_path) -> None:
    u = await _user(db_session)
    p = await _license_product(db_session)
    await license_service.add_keys(db_session, p.id, ["K"], actor_id=9)
    order = await _submitted_order(db_session, u, p, tmp_path)
    await db_session.commit()
    await payment_service.approve_payment(db_session, order.id, admin_id=None)
    await db_session.commit()
    with pytest.raises(ReceiptError) as ei:
        await payment_service.approve_payment(db_session, order.id, admin_id=None)
    assert ei.value.code == "not_reviewable"
    with pytest.raises(ReceiptError):
        await payment_service.reject_payment(db_session, order.id, admin_id=None, reason="x")


async def test_v2ray_delivery_via_mocked_panel(db_session, tmp_path, monkeypatch) -> None:
    from app.models import XuiInbound, XuiServer
    from app.services import xui_service

    server = XuiServer(name="S", base_url="http://p:2053", status="active", is_active=True)
    db_session.add(server)
    await db_session.flush()
    inbound = XuiInbound(server_id=server.id, inbound_id=7, remark="in", is_active=True)
    db_session.add(inbound)
    await db_session.flush()
    product = Product(type="v2ray", title="VPN", price=90_000, duration_days=30,
                      traffic_gb=50, ip_limit=2, is_active=True, is_hidden=False,
                      xui_server_id=server.id, xui_inbound_id=inbound.id)
    db_session.add(product)
    u = await _user(db_session, tg=222)
    await db_session.flush()

    called = {}

    async def fake_add_client(srv, inbound_id, client, **kw):
        called["inbound_id"] = inbound_id
        called["email"] = client.email
        return None

    monkeypatch.setattr(xui_service, "add_client", fake_add_client)

    order = await _submitted_order(db_session, u, product, tmp_path)
    await db_session.commit()
    result = await payment_service.approve_payment(db_session, order.id, admin_id=None)
    await db_session.commit()
    o = await order_service.get_order(db_session, order.id)
    assert o.status == "delivered"
    assert called["inbound_id"] == 7
    assert result["delivery"]["delivered"] is True

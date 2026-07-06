"""Phase 7: wallet balances, top-ups, wallet payment, refunds."""
from __future__ import annotations

import pytest
from sqlalchemy import select

from app.models import AuditLog, Order, Product, User, WalletTopupRequest, WalletTransaction
from app.services import license_service, order_service, payment_service, wallet_service
from app.services.payment_service import ReceiptFile
from app.services.wallet_service import InsufficientBalanceError, WalletError


@pytest.fixture(autouse=True)
def _stub_delivery(monkeypatch):
    async def _ok(bot, order, product, lic, lang="fa"):
        return True
    monkeypatch.setattr(license_service, "_deliver_to_user", _ok)


async def _user(db_session, tg=100, balance=0):
    u = User(telegram_id=tg, first_name="B", language="fa", wallet_balance=balance)
    db_session.add(u)
    await db_session.flush()
    return u


async def _license_product(db_session, price=50_000, with_stock=True):
    p = Product(type="license", title="Netflix", price=price, is_active=True, is_hidden=False)
    db_session.add(p)
    await db_session.flush()
    if with_stock:
        await license_service.add_license(db_session, p.id, "a@x.com", "pw", admin_id=9)
    return p


def _receipt():
    return ReceiptFile(content=b"\x89PNG\r\n\x1a\n" + b"x" * 30,
                       original_name="r.png", mime_type="image/png", file_id="f")


# --- top-up lifecycle -------------------------------------------------------
async def test_topup_create_submit_approve_credits(db_session, tmp_path) -> None:
    payment_service.RECEIPTS_ROOT = tmp_path
    u = await _user(db_session)
    topup = await wallet_service.create_topup_request(db_session, u.id, 20_000)
    assert topup.status == "pending_receipt"
    topup = await wallet_service.submit_topup_receipt(db_session, topup.id, u.id, _receipt())
    assert topup.status == "waiting_admin" and topup.receipt_path.startswith("wallet/")
    r = await wallet_service.approve_topup(db_session, topup.id, admin_id=9)
    assert r["ok"] and r["balance"] == 20_000
    assert await wallet_service.get_balance(db_session, u.id) == 20_000
    tx = (await db_session.execute(select(WalletTransaction))).scalars().all()
    assert len(tx) == 1 and tx[0].type == "deposit" and tx[0].topup_id == topup.id
    assert tx[0].balance_before == 0 and tx[0].balance_after == 20_000


async def test_reject_does_not_credit(db_session, tmp_path) -> None:
    payment_service.RECEIPTS_ROOT = tmp_path
    u = await _user(db_session)
    topup = await wallet_service.create_topup_request(db_session, u.id, 5_000)
    await wallet_service.submit_topup_receipt(db_session, topup.id, u.id, _receipt())
    r = await wallet_service.reject_topup(db_session, topup.id, admin_id=9, reason="blurry")
    assert r["ok"]
    assert await wallet_service.get_balance(db_session, u.id) == 0
    t = await wallet_service.get_topup(db_session, topup.id)
    assert t.status == "rejected" and t.reject_reason == "blurry"


async def test_approve_twice_fails(db_session, tmp_path) -> None:
    payment_service.RECEIPTS_ROOT = tmp_path
    u = await _user(db_session)
    topup = await wallet_service.create_topup_request(db_session, u.id, 5_000)
    await wallet_service.submit_topup_receipt(db_session, topup.id, u.id, _receipt())
    await wallet_service.approve_topup(db_session, topup.id, admin_id=9)
    with pytest.raises(WalletError):
        await wallet_service.approve_topup(db_session, topup.id, admin_id=9)
    assert await wallet_service.get_balance(db_session, u.id) == 5_000  # not doubled


async def test_reject_after_approve_fails(db_session, tmp_path) -> None:
    payment_service.RECEIPTS_ROOT = tmp_path
    u = await _user(db_session)
    topup = await wallet_service.create_topup_request(db_session, u.id, 5_000)
    await wallet_service.submit_topup_receipt(db_session, topup.id, u.id, _receipt())
    await wallet_service.approve_topup(db_session, topup.id, admin_id=9)
    with pytest.raises(WalletError):
        await wallet_service.reject_topup(db_session, topup.id, admin_id=9, reason="x")


async def test_reject_requires_reason(db_session, tmp_path) -> None:
    payment_service.RECEIPTS_ROOT = tmp_path
    u = await _user(db_session)
    topup = await wallet_service.create_topup_request(db_session, u.id, 5_000)
    await wallet_service.submit_topup_receipt(db_session, topup.id, u.id, _receipt())
    with pytest.raises(WalletError):
        await wallet_service.reject_topup(db_session, topup.id, admin_id=9, reason="  ")


async def test_topup_amount_validation(db_session) -> None:
    from app.models import Setting
    db_session.add_all([Setting(key="min_wallet_topup", value="1000"),
                        Setting(key="max_wallet_topup", value="100000")])
    await db_session.flush()
    u = await _user(db_session)
    with pytest.raises(WalletError):
        await wallet_service.create_topup_request(db_session, u.id, 0)
    with pytest.raises(WalletError):
        await wallet_service.create_topup_request(db_session, u.id, 500)   # below min
    with pytest.raises(WalletError):
        await wallet_service.create_topup_request(db_session, u.id, 200_000)  # above max
    ok = await wallet_service.create_topup_request(db_session, u.id, 50_000)
    assert ok.amount == 50_000


# --- wallet payment ---------------------------------------------------------
async def _wallet_order(db_session, u, p):
    order = await order_service.create_order(db_session, u.id, p.id, payment_method="wallet")
    return order


async def test_wallet_payment_charges_and_delivers(db_session) -> None:
    u = await _user(db_session, balance=100_000)
    p = await _license_product(db_session, price=50_000)
    order = await _wallet_order(db_session, u, p)
    r = await wallet_service.pay_order_with_wallet(db_session, order.id, u.id)
    assert r["ok"] and r["charged"] and r["balance"] == 50_000
    o = await order_service.get_order(db_session, order.id)
    assert o.status == "delivered"
    txs = (await db_session.execute(
        select(WalletTransaction).where(WalletTransaction.type == "purchase"))).scalars().all()
    assert len(txs) == 1 and txs[0].amount == -50_000 and txs[0].order_id == order.id
    assert r["delivery"]["delivered"] is True


async def test_wallet_payment_insufficient(db_session) -> None:
    u = await _user(db_session, balance=1_000)
    p = await _license_product(db_session, price=50_000)
    order = await _wallet_order(db_session, u, p)
    with pytest.raises(InsufficientBalanceError):
        await wallet_service.pay_order_with_wallet(db_session, order.id, u.id)
    assert await wallet_service.get_balance(db_session, u.id) == 1_000  # unchanged
    o = await order_service.get_order(db_session, order.id)
    assert o.status == "pending_payment"


async def test_wallet_payment_idempotent(db_session) -> None:
    u = await _user(db_session, balance=100_000)
    p = await _license_product(db_session, price=50_000)
    order = await _wallet_order(db_session, u, p)
    await wallet_service.pay_order_with_wallet(db_session, order.id, u.id)
    r2 = await wallet_service.pay_order_with_wallet(db_session, order.id, u.id)
    assert r2["already"] and not r2["charged"]
    assert await wallet_service.get_balance(db_session, u.id) == 50_000  # charged once
    txs = (await db_session.execute(
        select(WalletTransaction).where(WalletTransaction.type == "purchase"))).scalars().all()
    assert len(txs) == 1


async def test_wallet_payment_delivery_failure_keeps_charge(db_session) -> None:
    # No stock → license delivery fails, but the wallet charge stands (no auto-refund).
    u = await _user(db_session, balance=100_000)
    p = await _license_product(db_session, price=50_000, with_stock=False)
    order = await _wallet_order(db_session, u, p)
    r = await wallet_service.pay_order_with_wallet(db_session, order.id, u.id)
    assert r["charged"] and r["delivery"]["delivered"] is False
    assert await wallet_service.get_balance(db_session, u.id) == 50_000  # charged, not refunded
    o = await order_service.get_order(db_session, order.id)
    assert o.delivery_error


# --- refund -----------------------------------------------------------------
async def test_refund_credits_and_idempotent(db_session) -> None:
    u = await _user(db_session, balance=100_000)
    p = await _license_product(db_session, price=50_000)
    order = await _wallet_order(db_session, u, p)
    await wallet_service.pay_order_with_wallet(db_session, order.id, u.id)
    assert await wallet_service.get_balance(db_session, u.id) == 50_000
    r = await wallet_service.refund_wallet_payment(db_session, order.id, admin_id=9, reason="goodwill")
    assert r["ok"] and r["amount"] == 50_000
    assert await wallet_service.get_balance(db_session, u.id) == 100_000
    o = await order_service.get_order(db_session, order.id)
    assert o.refunded_at is not None and o.refund_reason == "goodwill"
    # Double refund is prevented.
    with pytest.raises(WalletError):
        await wallet_service.refund_wallet_payment(db_session, order.id, admin_id=9, reason="again")
    assert await wallet_service.get_balance(db_session, u.id) == 100_000


async def test_refund_requires_reason(db_session) -> None:
    u = await _user(db_session, balance=100_000)
    p = await _license_product(db_session, price=50_000)
    order = await _wallet_order(db_session, u, p)
    await wallet_service.pay_order_with_wallet(db_session, order.id, u.id)
    with pytest.raises(WalletError):
        await wallet_service.refund_wallet_payment(db_session, order.id, admin_id=9, reason="")


# --- security ---------------------------------------------------------------
async def test_audit_created_for_wallet_flows(db_session, tmp_path) -> None:
    payment_service.RECEIPTS_ROOT = tmp_path
    u = await _user(db_session, balance=100_000)
    p = await _license_product(db_session, price=50_000)
    order = await _wallet_order(db_session, u, p)
    await wallet_service.pay_order_with_wallet(db_session, order.id, u.id)
    actions = {r.action for r in (await db_session.execute(select(AuditLog))).scalars().all()}
    assert {"wallet_payment_started", "wallet_payment_completed"} <= actions

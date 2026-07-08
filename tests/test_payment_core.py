"""Payment Core slice 1: renderer, invoices, methods, wallet pay, receipts,
approve/reject idempotency, expiry + cleanup. All service-level (no bot/web)."""
from __future__ import annotations

from datetime import datetime, timedelta, timezone

import pytest

from app.models.invoice import Invoice
from app.models.license_item import LicenseItem
from app.models.payment import Payment
from app.models.payment_method import PaymentMethod
from app.models.product import Product
from app.models.user import User
from app.services import (
    payment_cleanup_service,
    payment_core_service as core,
    payment_service,
    order_service,
    wallet_service,
)
from app.services.template_render_service import format_toman, render_text_template


# --------------------------------------------------------------------------
# Fixtures / helpers
# --------------------------------------------------------------------------
async def _seed_methods(session) -> None:
    session.add_all([
        PaymentMethod(code="wallet", title="کیف پول", method_type="wallet",
                      is_active=True, sort_order=1),
        PaymentMethod(code="manual_receipt", title="کارت به کارت",
                      method_type="manual_receipt", is_active=True, sort_order=2),
        PaymentMethod(code="online_gateway", title="درگاه آنلاین",
                      method_type="online_gateway", is_active=False, sort_order=3),
    ])
    await session.commit()


async def _make_user(session, *, balance: int = 0, tg: int = 111) -> User:
    user = User(telegram_id=tg, first_name="Test", username="tester",
                wallet_balance=balance)
    session.add(user)
    await session.commit()
    await session.refresh(user)
    return user


async def _make_license_product(session, *, price: int = 50_000) -> Product:
    product = Product(type="license", title="لایسنس تست", price=price,
                      is_active=True, stock_count=5)
    session.add(product)
    await session.flush()
    session.add(LicenseItem(product_id=product.id, email="user@example.com",
                            password="secret", status="available"))
    await session.commit()
    await session.refresh(product)
    return product


def _fake_file(name="receipt.jpg", mime="image/jpeg") -> payment_service.ReceiptFile:
    return payment_service.ReceiptFile(
        content=b"\xff\xd8fakejpegbytes", original_name=name, mime_type=mime,
        file_id="tg-file-1",
    )


class _FakeBot:
    def __init__(self) -> None:
        self.sent: list[dict] = []

    async def send_message(self, **kwargs):
        self.sent.append(kwargs)

    async def send_photo(self, **kwargs):
        self.sent.append(kwargs)

    async def send_document(self, **kwargs):
        self.sent.append(kwargs)


# --------------------------------------------------------------------------
# Template renderer
# --------------------------------------------------------------------------
def test_render_replaces_known_variables() -> None:
    out = render_text_template("سلام {username}، قیمت: {price} تومان",
                               {"username": "ali", "price": "1,000"})
    assert out == "سلام ali، قیمت: 1,000 تومان"


def test_render_keeps_unknown_variables_visible() -> None:
    out = render_text_template("x {unknown_var} y {username}", {"username": "u"})
    assert "{unknown_var}" in out and "u" in out


def test_render_none_becomes_empty_and_no_eval() -> None:
    out = render_text_template("n:{note} e:{__import__}", {"note": None})
    assert out.startswith("n: ")
    assert "{__import__}" in out  # dunder stays a literal, nothing executed


def test_render_invoice_template_with_product_data() -> None:
    template = ("🧾 {name_product} · {Service_time} روز · {price} تومان · "
                "{Volume} · {userBalance}")
    out = render_text_template(template, {
        "name_product": "پلن ۳۰ روزه", "Service_time": 30,
        "price": format_toman(250000), "Volume": "50 GB",
        "userBalance": format_toman(120000),
    })
    assert "پلن ۳۰ روزه" in out and "250,000" in out and "120,000" in out


def test_render_manual_receipt_template_with_card_data() -> None:
    out = render_text_template("{price} → {card_number} ({name_card})",
                               {"price": "90,000", "card_number": "6037-1234",
                                "name_card": "علی رضایی"})
    assert "6037-1234" in out and "علی رضایی" in out


# --------------------------------------------------------------------------
# Tracking codes / invoices
# --------------------------------------------------------------------------
def test_tracking_codes_unique_and_prefixed() -> None:
    codes = {core.generate_tracking_code() for _ in range(200)}
    assert len(codes) == 200
    assert all(c.startswith("PAY-") and len(c) == 14 for c in codes)


async def test_create_product_invoice_and_reuse(db_session) -> None:
    await _seed_methods(db_session)
    user = await _make_user(db_session)
    product = await _make_license_product(db_session)
    order = await order_service.create_order(db_session, user.id, product.id)

    inv1 = await core.create_product_invoice(db_session, user, product, order=order)
    await db_session.commit()
    assert inv1.status == "unpaid" and inv1.final_amount == product.price
    assert inv1.invoice_number.startswith("INV-")

    inv2 = await core.create_product_invoice(db_session, user, product, order=order)
    assert inv2.id == inv1.id  # reused, not duplicated


async def test_wallet_topup_invoice_min_max(db_session) -> None:
    user = await _make_user(db_session)
    from app.core.settings_service import SettingsService
    svc = SettingsService(db_session)
    await svc.set("min_wallet_topup", "10000", actor_type="system")
    await svc.set("max_wallet_topup", "500000", actor_type="system")
    await db_session.commit()

    with pytest.raises(core.PaymentCoreError):
        await core.create_wallet_topup_invoice(db_session, user, 5000)
    with pytest.raises(core.PaymentCoreError):
        await core.create_wallet_topup_invoice(db_session, user, 600000)
    inv = await core.create_wallet_topup_invoice(db_session, user, 50000)
    await db_session.commit()
    assert inv.invoice_type == "wallet_topup" and inv.final_amount == 50000


# --------------------------------------------------------------------------
# Active methods + bonus
# --------------------------------------------------------------------------
async def test_active_methods_filtering(db_session) -> None:
    await _seed_methods(db_session)
    user = await _make_user(db_session)
    methods = await core.get_active_payment_methods(db_session, 50000, user)
    codes = [m.code for m in methods]
    assert codes == ["wallet", "manual_receipt"]  # inactive gateway excluded


def test_gateway_bonus_floor() -> None:
    method = PaymentMethod(code="x", title="x", method_type="online_gateway",
                           cashback_percent=5)
    assert core.calculate_gateway_bonus(method, 100_000) == 5000
    assert core.calculate_gateway_bonus(None, 100_000) == 0


# --------------------------------------------------------------------------
# Wallet payment
# --------------------------------------------------------------------------
async def test_wallet_pay_deducts_and_marks_paid(db_session) -> None:
    await _seed_methods(db_session)
    user = await _make_user(db_session, balance=100_000)
    product = await _make_license_product(db_session, price=60_000)
    order = await order_service.create_order(db_session, user.id, product.id,
                                             payment_method="wallet")
    invoice = await core.create_product_invoice(db_session, user, product, order=order)
    await db_session.commit()

    result = await core.pay_invoice_with_wallet(db_session, invoice.id, user.id)
    assert result["ok"] and result["charged"]
    assert result["balance"] == 40_000
    assert result["tracking_code"] and result["tracking_code"].startswith("PAY-")
    invoice = await core.get_invoice(db_session, invoice.id)
    assert invoice.status == "paid" and invoice.paid_at is not None


async def test_wallet_pay_insufficient_fails_safely(db_session) -> None:
    await _seed_methods(db_session)
    user = await _make_user(db_session, balance=1000)
    product = await _make_license_product(db_session, price=60_000)
    order = await order_service.create_order(db_session, user.id, product.id,
                                             payment_method="wallet")
    invoice = await core.create_product_invoice(db_session, user, product, order=order)
    await db_session.commit()

    with pytest.raises(wallet_service.InsufficientBalanceError):
        await core.pay_invoice_with_wallet(db_session, invoice.id, user.id)
    await db_session.rollback()
    await db_session.refresh(user)
    assert int(user.wallet_balance) == 1000  # nothing deducted
    invoice = await core.get_invoice(db_session, invoice.id)
    assert invoice.status == "unpaid"


# --------------------------------------------------------------------------
# Manual receipt: top-up payment lifecycle
# --------------------------------------------------------------------------
async def _pending_topup_payment(db_session, user, amount=80_000):
    invoice = await core.create_wallet_topup_invoice(db_session, user, amount)
    payment = await core.create_payment(db_session, invoice, "manual_receipt")
    await db_session.commit()
    return invoice, payment


async def test_manual_topup_payment_created_pending(db_session) -> None:
    await _seed_methods(db_session)
    user = await _make_user(db_session)
    invoice, payment = await _pending_topup_payment(db_session, user)
    assert payment.status == "pending" and payment.order_id is None
    assert payment.payment_type == "wallet_topup"
    assert payment.tracking_code.startswith("PAY-")
    assert payment.invoice_id == invoice.id


async def test_receipt_submission_updates_payment(db_session, tmp_path, monkeypatch) -> None:
    monkeypatch.setattr(payment_service, "RECEIPTS_ROOT", tmp_path)
    await _seed_methods(db_session)
    user = await _make_user(db_session)
    _, payment = await _pending_topup_payment(db_session, user)

    updated = await core.submit_manual_receipt(db_session, payment.id, user.id, _fake_file())
    await db_session.commit()
    assert updated.status == "receipt_submitted"
    assert updated.submitted_at is not None and updated.receipt_path
    assert (tmp_path / updated.receipt_path).exists()


async def test_approve_topup_credits_wallet_with_bonus(db_session, tmp_path, monkeypatch) -> None:
    monkeypatch.setattr(payment_service, "RECEIPTS_ROOT", tmp_path)
    await _seed_methods(db_session)
    # Give the manual method 10% cashback.
    method = await core.get_method_by_code(db_session, "manual_receipt")
    method.cashback_percent = 10
    await db_session.commit()

    user = await _make_user(db_session, balance=0)
    _, payment = await _pending_topup_payment(db_session, user, amount=100_000)
    assert payment.bonus_amount == 10_000
    await core.submit_manual_receipt(db_session, payment.id, user.id, _fake_file())
    await db_session.commit()

    result = await core.approve_payment(db_session, payment.id, admin_id=None)
    assert result["ok"] and result["credited"] == 110_000
    await db_session.refresh(user)
    assert int(user.wallet_balance) == 110_000

    # Idempotent: second approval does not double-credit.
    again = await core.approve_payment(db_session, payment.id, admin_id=None)
    assert again["already"] is True
    await db_session.refresh(user)
    assert int(user.wallet_balance) == 110_000


async def test_approve_product_payment_delivers_once(db_session, tmp_path, monkeypatch) -> None:
    monkeypatch.setattr(payment_service, "RECEIPTS_ROOT", tmp_path)
    calls = []

    async def _fake_deliver(session, order, **kw):
        calls.append(order.id)
        return {"ok": True, "delivered": True, "reason": "test"}

    from app.services import delivery_service
    monkeypatch.setattr(delivery_service, "deliver_order", _fake_deliver)

    await _seed_methods(db_session)
    user = await _make_user(db_session)
    product = await _make_license_product(db_session)
    order = await order_service.create_order(db_session, user.id, product.id)
    invoice = await core.create_product_invoice(db_session, user, product, order=order)
    payment = await core.create_payment(db_session, invoice, "manual_receipt")
    await db_session.commit()
    await core.submit_manual_receipt(db_session, payment.id, user.id, _fake_file())
    await db_session.commit()

    result = await core.approve_payment(db_session, payment.id, admin_id=None)
    assert result["ok"] and len(calls) == 1
    invoice = await core.get_invoice(db_session, invoice.id)
    assert invoice.status == "paid"

    # Second approval: refused as already approved, delivery NOT re-run.
    again = await core.approve_payment(db_session, payment.id, admin_id=None)
    assert again["already"] is True and len(calls) == 1


async def test_reject_stores_reason(db_session, tmp_path, monkeypatch) -> None:
    monkeypatch.setattr(payment_service, "RECEIPTS_ROOT", tmp_path)
    await _seed_methods(db_session)
    user = await _make_user(db_session)
    _, payment = await _pending_topup_payment(db_session, user)
    await core.submit_manual_receipt(db_session, payment.id, user.id, _fake_file())
    await db_session.commit()

    with pytest.raises(core.PaymentCoreError):
        await core.reject_payment(db_session, payment.id, admin_id=None, reason="  ")
    result = await core.reject_payment(db_session, payment.id, admin_id=None,
                                       reason="مبلغ اشتباه است")
    assert result["payment"].status == "rejected"
    assert result["payment"].reject_reason == "مبلغ اشتباه است"
    # A rejected payment cannot be approved afterwards.
    with pytest.raises(core.PaymentCoreError):
        await core.approve_payment(db_session, payment.id, admin_id=None)


async def test_expired_pending_payment_cannot_be_approved(db_session) -> None:
    await _seed_methods(db_session)
    user = await _make_user(db_session)
    _, payment = await _pending_topup_payment(db_session, user)
    payment.created_at = datetime.now(timezone.utc) - timedelta(days=3)
    await db_session.commit()

    expired = await core.expire_old_pending_payments(db_session, 1)
    assert expired == 1
    with pytest.raises(core.PaymentCoreError):
        await core.approve_payment(db_session, payment.id, admin_id=None)


# --------------------------------------------------------------------------
# Cleanup
# --------------------------------------------------------------------------
async def test_cleanup_expires_old_but_not_paid(db_session) -> None:
    await _seed_methods(db_session)
    user = await _make_user(db_session)
    old = datetime.now(timezone.utc) - timedelta(days=10)

    inv_old = await core.create_wallet_topup_invoice(db_session, user, 10_000)
    inv_old.created_at = old
    inv_paid = await core.create_wallet_topup_invoice(db_session, user, 20_000)
    inv_paid.created_at = old
    inv_paid.status = "paid"
    pay_old = Payment(user_id=user.id, amount=10_000, status="pending", created_at=old)
    pay_ok = Payment(user_id=user.id, amount=10_000, status="approved", created_at=old,
                     tracking_code="PAY-KEEPME1234")
    db_session.add_all([pay_old, pay_ok])
    await db_session.commit()

    bot = _FakeBot()
    result = await payment_cleanup_service.run_payment_cleanup(db_session, bot=bot)
    assert result["invoices_expired"] == 1 and result["payments_expired"] == 1

    await db_session.refresh(inv_old); await db_session.refresh(inv_paid)
    await db_session.refresh(pay_old); await db_session.refresh(pay_ok)
    assert inv_old.status == "expired" and inv_paid.status == "paid"
    assert pay_old.status == "expired" and pay_ok.status == "approved"
    # No chat configured -> summary send skipped quietly, flow unaffected.
    assert bot.sent == []


async def test_cleanup_log_sent_when_chat_configured(db_session) -> None:
    from app.core.settings_service import SettingsService
    await SettingsService(db_session).set(
        "general_log_chat_id", "-100123", actor_type="system")
    user = await _make_user(db_session)
    inv = await core.create_wallet_topup_invoice(db_session, user, 10_000)
    inv.created_at = datetime.now(timezone.utc) - timedelta(days=10)
    await db_session.commit()

    bot = _FakeBot()
    result = await payment_cleanup_service.run_payment_cleanup(db_session, bot=bot)
    assert result["invoices_expired"] == 1
    assert len(bot.sent) == 1
    assert "پاکسازی خودکار سیستم" in bot.sent[0]["text"]

"""Payment Core (slice 1): invoices, tracking codes, and one payment façade.

The single entry point the bot/web use for the new invoice-driven money flow.
It NEVER re-implements money movement — it composes the proven services:

  * product purchases  → payment_service (receipt approve/reject, which runs the
    delivery dispatcher exactly once) and wallet_service.pay_order_with_wallet
    (atomic wallet charge under a user-row lock);
  * wallet top-ups     → a Payment row with no order; approval credits the wallet
    atomically via wallet_service.add_balance (+ gateway cashback bonus).

Every invoice/payment gets a unique tracking code. Approvals are idempotent: a
paid payment returns {"already": True}, a rejected/expired one is refused.
Future gateways plug in through PaymentMethod rows (encrypted credentials) and
the provider_* fields — no schema change needed later.
"""
from __future__ import annotations

import json
import logging
import secrets
from datetime import datetime, timedelta, timezone

from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.settings_service import SettingsService
from app.models.invoice import Invoice
from app.models.order import Order
from app.models.payment import Payment
from app.models.payment_method import PaymentMethod
from app.models.user import User
from app.services import audit_service, payment_service, wallet_service

log = logging.getLogger("payment_core")

# Unambiguous alphabet for human-facing codes (no 0/O/1/I).
_CODE_ALPHABET = "23456789ABCDEFGHJKMNPQRSTUVWXYZ"

# Which settings kill-switch gates each method type (None = always allowed).
_METHOD_TYPE_SETTING: dict[str, str | None] = {
    "wallet": "wallet_payment_enabled",
    "manual_receipt": "card_to_card_enabled",
    "online_gateway": "online_gateway_enabled",
    "custom_gateway": "custom_gateway_enabled",
    "crypto": None,
    "telegram_stars": None,
}


class PaymentCoreError(ValueError):
    code = "payment_core_error"

    def __init__(self, message: str, code: str | None = None) -> None:
        super().__init__(message)
        if code is not None:
            self.code = code


def _now() -> datetime:
    return datetime.now(timezone.utc)


def generate_tracking_code(prefix: str = "PAY") -> str:
    """A unique, human-friendly tracking code like ``PAY-7K2M9QWD4X``."""
    body = "".join(secrets.choice(_CODE_ALPHABET) for _ in range(10))
    return f"{prefix}-{body}"


def generate_invoice_number() -> str:
    when = _now()
    body = "".join(secrets.choice(_CODE_ALPHABET) for _ in range(6))
    return f"INV-{when:%Y%m%d}-{body}"


def calculate_gateway_bonus(method: PaymentMethod | None, amount: int) -> int:
    """Wallet-credit cashback for paying `amount` through `method` (floor)."""
    if method is None:
        return 0
    percent = float(method.cashback_percent or 0)
    if percent <= 0:
        return 0
    return int(int(amount) * percent // 100)


# --------------------------------------------------------------------------
# Invoices
# --------------------------------------------------------------------------
async def get_invoice(session: AsyncSession, invoice_id: int) -> Invoice | None:
    return await session.get(Invoice, invoice_id)


async def get_invoice_by_order(session: AsyncSession, order_id: int) -> Invoice | None:
    stmt = (select(Invoice).where(Invoice.order_id == order_id)
            .order_by(Invoice.id.desc()).limit(1))
    return await session.scalar(stmt)


async def create_product_invoice(
    session: AsyncSession, user: User, product, *, order: Order | None = None,
    coupon=None,
) -> Invoice:
    """Create (or reuse) the unpaid invoice for a product order.

    Amounts come from the order when given (it already carries any coupon
    discount); otherwise from the product's list price. Reuse keeps the flow
    idempotent: re-opening the pre-invoice never mints a second document.
    """
    if order is not None:
        existing = await get_invoice_by_order(session, order.id)
        if existing is not None and existing.status == "unpaid":
            # Keep amounts in sync (a coupon may have been applied meanwhile).
            existing.amount = int(order.amount or 0)
            existing.discount_amount = int(order.discount_amount or 0)
            existing.final_amount = int(order.final_amount or 0)
            await session.flush()
            return existing

    amount = int(order.amount if order is not None else (product.price or 0))
    discount = int(order.discount_amount or 0) if order is not None else 0
    final = int(order.final_amount if order is not None else amount - discount)

    invoice_type = "product_purchase"
    if order is not None and order.action_type in ("renew_service", "add_traffic"):
        invoice_type = ("renewal" if order.action_type == "renew_service"
                        else "add_traffic")

    invoice = Invoice(
        invoice_number=generate_invoice_number(),
        tracking_code=generate_tracking_code("INV"),
        user_id=user.id,
        order_id=order.id if order is not None else None,
        product_id=getattr(product, "id", None),
        invoice_type=invoice_type,
        amount=amount, discount_amount=discount, final_amount=final,
        status="unpaid",
    )
    session.add(invoice)
    await session.flush()
    await audit_service.log(
        session, actor_type="user", actor_id=user.id, action="invoice_created",
        target_type="invoice", target_id=invoice.id,
        new=f"number={invoice.invoice_number} type={invoice_type} amount={final}",
    )
    return invoice


async def create_wallet_topup_invoice(
    session: AsyncSession, user: User, amount: int
) -> Invoice:
    """Create an unpaid wallet top-up invoice (validates the min/max settings)."""
    amount = int(amount)
    if amount <= 0:
        raise PaymentCoreError("amount must be positive", code="bad_amount")
    svc = SettingsService(session)
    min_topup = await svc.get_int("min_wallet_topup", 0)
    max_topup = await svc.get_int("max_wallet_topup", 0)
    if min_topup and amount < min_topup:
        raise PaymentCoreError(f"minimum top-up is {min_topup}", code="below_min")
    if max_topup and amount > max_topup:
        raise PaymentCoreError(f"maximum top-up is {max_topup}", code="above_max")

    invoice = Invoice(
        invoice_number=generate_invoice_number(),
        tracking_code=generate_tracking_code("INV"),
        user_id=user.id, invoice_type="wallet_topup",
        amount=amount, discount_amount=0, final_amount=amount,
        status="unpaid",
    )
    session.add(invoice)
    await session.flush()
    await audit_service.log(
        session, actor_type="user", actor_id=user.id, action="invoice_created",
        target_type="invoice", target_id=invoice.id,
        new=f"number={invoice.invoice_number} type=wallet_topup amount={amount}",
    )
    return invoice


async def _mark_invoice_paid(session: AsyncSession, invoice: Invoice | None) -> None:
    if invoice is not None and invoice.status == "unpaid":
        invoice.status = "paid"
        invoice.paid_at = _now()


# --------------------------------------------------------------------------
# Payment methods
# --------------------------------------------------------------------------
async def get_method_by_code(session: AsyncSession, code: str) -> PaymentMethod | None:
    return await session.scalar(
        select(PaymentMethod).where(PaymentMethod.code == code))


async def list_methods(session: AsyncSession) -> list[PaymentMethod]:
    stmt = select(PaymentMethod).order_by(PaymentMethod.sort_order, PaymentMethod.id)
    return list((await session.execute(stmt)).scalars().all())


async def _count_approved_payments(session: AsyncSession, user_id: int) -> int:
    return int(await session.scalar(
        select(func.count(Payment.id)).where(
            Payment.user_id == user_id, Payment.status == "approved")
    ) or 0)


async def get_active_payment_methods(
    session: AsyncSession, amount: int, user: User | None = None,
    *, exclude_wallet: bool = False,
) -> list[PaymentMethod]:
    """Active methods this user may pay `amount` with, in sort order.

    Filters: is_active, the per-type settings kill-switch, the min/max amount
    window, and `activate_after_payments` (needs N prior approved payments).
    """
    svc = SettingsService(session)
    out: list[PaymentMethod] = []
    dropped: list[str] = []
    approved_count: int | None = None
    for method in await list_methods(session):
        if not method.is_active:
            dropped.append(f"{method.code}:inactive")
            continue
        if exclude_wallet and method.method_type == "wallet":
            dropped.append(f"{method.code}:wallet-excluded")
            continue
        setting_key = _METHOD_TYPE_SETTING.get(method.method_type)
        if setting_key and not await svc.get_bool(setting_key, True):
            dropped.append(f"{method.code}:setting-off({setting_key})")
            continue
        if method.min_amount and amount < int(method.min_amount):
            dropped.append(f"{method.code}:below-min({method.min_amount})")
            continue
        if method.max_amount and amount > int(method.max_amount):
            dropped.append(f"{method.code}:above-max({method.max_amount})")
            continue
        needed = int(method.activate_after_payments or 0)
        if needed > 0:
            if user is None:
                dropped.append(f"{method.code}:needs-{needed}-payments(anon)")
                continue
            if approved_count is None:
                approved_count = await _count_approved_payments(session, user.id)
            if approved_count < needed:
                dropped.append(f"{method.code}:needs-{needed}-has-{approved_count}")
                continue
        out.append(method)
    # Debug-safe: codes + reasons only, never secrets.
    log.info("active payment methods amount=%s exclude_wallet=%s -> %d shown [%s]; dropped [%s]",
             amount, exclude_wallet, len(out), ",".join(m.code for m in out) or "-",
             ",".join(dropped) or "-")
    return out


# ---------------------------------------------------------------------------
# Bot-facing resolver: ordered method_types to OFFER, with a settings-derived
# fallback when the PaymentMethod table has no rows (fresh/older DB where the
# 0021 seed never ran). Keeps the bot from ever showing zero methods.
# ---------------------------------------------------------------------------
_FALLBACK_ORDER: tuple[str, ...] = (
    "wallet", "manual_receipt", "online_gateway", "custom_gateway",
)


async def _any_methods(session: AsyncSession) -> bool:
    return bool(await session.scalar(select(func.count(PaymentMethod.id))) or 0)


async def _settings_fallback_types(
    session: AsyncSession, *, exclude_wallet: bool
) -> list[str]:
    """Legacy settings-driven method types (used only when no rows are seeded)."""
    svc = SettingsService(session)
    types: list[str] = []
    if not exclude_wallet and await svc.get_bool("wallet_enabled", True) \
            and await svc.get_bool("wallet_payment_enabled", True):
        types.append("wallet")
    if await svc.get_bool("card_to_card_enabled", True) \
            and (await svc.get_str("card_number", "")).strip():
        types.append("manual_receipt")
    if await svc.get_bool("online_gateway_enabled", False):
        types.append("online_gateway")
    if await svc.get_bool("custom_gateway_enabled", False):
        types.append("custom_gateway")
    return types


async def resolve_method_types(
    session: AsyncSession, amount: int, user: User | None = None,
    *, exclude_wallet: bool = False,
) -> list[str]:
    """Ordered payment `method_type`s to offer for `amount`.

    Table-driven via :func:`get_active_payment_methods`; if the table is empty
    (never seeded) it falls back to the settings-derived defaults so the bot
    still offers wallet / card / enabled gateways instead of nothing.
    """
    amount = int(amount)
    if await _any_methods(session):
        methods = await get_active_payment_methods(
            session, amount, user, exclude_wallet=exclude_wallet)
        types = [m.method_type for m in methods]
        source = "db"
    else:
        types = await _settings_fallback_types(session, exclude_wallet=exclude_wallet)
        source = "settings-fallback"
    log.info("resolve_method_types amount=%s exclude_wallet=%s via %s -> %s",
             amount, exclude_wallet, source, ",".join(types) or "none")
    return types


# --------------------------------------------------------------------------
# Payments
# --------------------------------------------------------------------------
async def get_payment(session: AsyncSession, payment_id: int) -> Payment | None:
    return await session.get(Payment, payment_id)


async def create_payment(
    session: AsyncSession, invoice: Invoice, method_code: str
) -> Payment:
    """Create (or adopt) the pending Payment that settles `invoice` via `method`.

    Product invoices adopt the order's existing Payment row (the platform's
    one-payment-per-order money row) instead of minting a parallel one; wallet
    top-up invoices get a fresh order-less Payment.
    """
    if invoice.status != "unpaid":
        raise PaymentCoreError("invoice is not payable", code="not_payable")
    method = await get_method_by_code(session, method_code)
    if method is None or not method.is_active:
        raise PaymentCoreError("payment method unavailable", code="method_unavailable")

    payment: Payment | None = None
    if invoice.order_id is not None:
        payment = await payment_service.get_payment_by_order(session, invoice.order_id)
        if payment is not None and payment.status not in ("pending",):
            raise PaymentCoreError("order already has an active payment",
                                   code="already_paying")

    if payment is None:
        payment = Payment(
            order_id=invoice.order_id, user_id=invoice.user_id,
            amount=int(invoice.final_amount or 0), status="pending",
        )
        session.add(payment)

    payment.invoice_id = invoice.id
    payment.payment_type = invoice.invoice_type
    payment.method = ("card_to_card" if method.method_type == "manual_receipt"
                      else method.method_type)
    payment.provider_name = method.code
    payment.amount = int(invoice.final_amount or 0)
    payment.bonus_amount = (
        calculate_gateway_bonus(method, payment.amount)
        if invoice.invoice_type == "wallet_topup" else 0
    )
    if not payment.tracking_code:
        payment.tracking_code = generate_tracking_code("PAY")
    await session.flush()
    await audit_service.log(
        session, actor_type="user", actor_id=invoice.user_id, action="payment_created",
        target_type="payment", target_id=payment.id,
        new=f"invoice={invoice.invoice_number} method={method.code} "
            f"amount={payment.amount} tracking={payment.tracking_code}",
    )
    return payment


async def pay_invoice_with_wallet(
    session: AsyncSession, invoice_id: int, user_id: int, *, bot=None
) -> dict:
    """Settle a product invoice from the wallet (atomic; delegates the charge +
    delivery to wallet_service.pay_order_with_wallet, which locks the user row)."""
    invoice = await get_invoice(session, invoice_id)
    if invoice is None:
        raise PaymentCoreError("invoice not found", code="invoice_not_found")
    if invoice.user_id != user_id:
        raise PaymentCoreError("not your invoice", code="not_your_invoice")
    if invoice.status == "paid":
        return {"ok": True, "already": True, "invoice_id": invoice.id}
    if invoice.status != "unpaid":
        raise PaymentCoreError("invoice is not payable", code="not_payable")
    if invoice.order_id is None:
        raise PaymentCoreError("wallet cannot settle a wallet top-up invoice",
                               code="wallet_topup_via_wallet")

    result = await wallet_service.pay_order_with_wallet(
        session, invoice.order_id, user_id, bot=bot
    )
    # Stamp the invoice + tracking metadata after the atomic charge.
    invoice = await get_invoice(session, invoice_id)
    await _mark_invoice_paid(session, invoice)
    payment = await payment_service.get_payment_by_order(session, invoice.order_id)
    if payment is not None:
        payment.invoice_id = invoice.id
        payment.payment_type = invoice.invoice_type
        if not payment.tracking_code:
            payment.tracking_code = generate_tracking_code("PAY")
    await session.commit()
    result["invoice_id"] = invoice.id
    result["tracking_code"] = payment.tracking_code if payment else None
    return result


async def submit_manual_receipt(
    session: AsyncSession, payment_id: int, user_id: int,
    file_info: payment_service.ReceiptFile,
) -> Payment:
    """Attach a card-to-card receipt to a pending payment (photo/image/PDF).

    Product payments delegate to the existing order receipt flow (which also
    moves the order to waiting_admin); top-up payments store the receipt on the
    Payment row itself using the same validation + storage conventions.
    """
    payment = await get_payment(session, payment_id)
    if payment is None:
        raise payment_service.ReceiptError("payment not found", code="payment_not_found")
    if payment.user_id != user_id:
        raise payment_service.ReceiptError("not your payment", code="not_your_payment")

    if payment.order_id is not None:
        return await payment_service.submit_receipt(
            session, payment.order_id, user_id, file_info)

    if payment.status != "pending":
        raise payment_service.ReceiptError("a receipt was already submitted",
                                           code="already_submitted")
    ext = payment_service.validate_receipt(
        file_info.original_name, file_info.size, file_info.mime_type)
    when = _now()
    ref = payment.tracking_code or f"payment-{payment.id}"
    rel = payment_service.build_receipt_relpath(
        ref, file_info.original_name, file_info.mime_type, when)
    dest = payment_service.RECEIPTS_ROOT / rel
    dest.parent.mkdir(parents=True, exist_ok=True)
    dest.write_bytes(file_info.content)

    payment.receipt_path = rel
    payment.receipt_file_id = file_info.file_id
    payment.receipt_mime_type = (file_info.mime_type
                                 or payment_service._MIME_FOR_EXT.get(ext))
    payment.receipt_original_name = file_info.original_name
    payment.receipt_size = file_info.size
    payment.status = "receipt_submitted"
    payment.submitted_at = when
    await audit_service.log(
        session, actor_type="user", actor_id=user_id, action="receipt_submitted",
        target_type="payment", target_id=payment.id,
        meta=f"tracking={payment.tracking_code} size={file_info.size}",
    )
    await session.refresh(payment)
    return payment


# --------------------------------------------------------------------------
# Approve / reject (idempotent)
# --------------------------------------------------------------------------
async def approve_payment(
    session: AsyncSession, payment_id: int, admin_id: int | None, *, bot=None
) -> dict:
    """Approve a submitted payment. Idempotent: an approved payment returns
    already=True; a rejected/expired/cancelled one raises (needs a new payment)."""
    payment = await get_payment(session, payment_id)
    if payment is None:
        raise PaymentCoreError("payment not found", code="payment_not_found")
    if payment.status == "approved":
        return {"ok": True, "already": True, "payment": payment}
    if payment.status in ("rejected", "expired", "cancelled", "failed"):
        raise PaymentCoreError("payment can no longer be approved",
                               code="not_approvable")

    invoice = (await get_invoice(session, payment.invoice_id)
               if payment.invoice_id else None)

    if payment.order_id is not None:
        # Product purchase: the existing approve runs coupon + referral + the
        # delivery dispatcher EXACTLY once and never double-delivers.
        result = await payment_service.approve_payment(
            session, payment.order_id, admin_id=admin_id, bot=bot)
        await _mark_invoice_paid(session, invoice)
        await session.commit()
        return {"ok": True, "already": False, "payment": result["payment"],
                "order": result["order"], "delivery": result["delivery"]}

    # Wallet top-up: credit amount (+ gateway bonus) atomically under the
    # user-row lock inside add_balance.
    if payment.status != "receipt_submitted":
        raise PaymentCoreError("payment is not awaiting review", code="not_reviewable")
    credit = int(payment.amount or 0) + int(payment.bonus_amount or 0)
    await wallet_service.add_balance(
        session, payment.user_id, credit, admin_id=admin_id,
        reason=f"top-up {payment.tracking_code or payment.id}",
        payment_id=payment.id, transaction_type="topup_approved",
    )
    now = _now()
    payment.status = "approved"
    payment.approved_at = now
    payment.admin_id = admin_id
    payment.final_wallet_credit = credit
    await _mark_invoice_paid(session, invoice)
    await audit_service.log(
        session, actor_type="admin", actor_id=admin_id, action="wallet_topup_payment_approved",
        target_type="payment", target_id=payment.id,
        meta=f"credit={credit} bonus={payment.bonus_amount} "
             f"tracking={payment.tracking_code}",
    )
    await session.commit()
    await session.refresh(payment)
    return {"ok": True, "already": False, "payment": payment, "credited": credit}


async def reject_payment(
    session: AsyncSession, payment_id: int, admin_id: int | None, reason: str
) -> dict:
    """Reject a submitted payment with a required reason (sent to the user by
    the caller). A product rejection also rejects its order via the existing flow."""
    if not (reason or "").strip():
        raise PaymentCoreError("a reason is required", code="reason_required")
    payment = await get_payment(session, payment_id)
    if payment is None:
        raise PaymentCoreError("payment not found", code="payment_not_found")
    if payment.status == "rejected":
        return {"ok": True, "already": True, "payment": payment}
    if payment.status == "approved":
        raise PaymentCoreError("an approved payment cannot be rejected",
                               code="already_approved")

    if payment.order_id is not None:
        result = await payment_service.reject_payment(
            session, payment.order_id, admin_id=admin_id, reason=reason)
        result["payment"].reject_reason = reason.strip()
        await session.commit()
        return {"ok": True, "already": False, "payment": result["payment"],
                "order": result["order"]}

    if payment.status != "receipt_submitted":
        raise PaymentCoreError("payment is not awaiting review", code="not_reviewable")
    now = _now()
    payment.status = "rejected"
    payment.rejected_at = now
    payment.admin_id = admin_id
    payment.reject_reason = reason.strip()
    await audit_service.log(
        session, actor_type="admin", actor_id=admin_id, action="wallet_topup_payment_rejected",
        target_type="payment", target_id=payment.id,
        meta=f"tracking={payment.tracking_code} reason={reason.strip()}",
    )
    await session.commit()
    await session.refresh(payment)
    return {"ok": True, "already": False, "payment": payment}


# --------------------------------------------------------------------------
# Expiry (cleanup)
# --------------------------------------------------------------------------
async def expire_old_unpaid_invoices(session: AsyncSession, days: int) -> int:
    """Mark unpaid invoices older than `days` as expired. Returns the count."""
    if days <= 0:
        return 0
    cutoff = _now() - timedelta(days=days)
    rows = list((await session.execute(
        select(Invoice).where(Invoice.status == "unpaid",
                              Invoice.created_at < cutoff)
    )).scalars().all())
    now = _now()
    for inv in rows:
        inv.status = "expired"
        inv.cancelled_at = now
    if rows:
        await audit_service.log(
            session, actor_type="system", actor_id=None, action="invoices_expired",
            target_type="invoice", target_id=None, meta=f"count={len(rows)} days={days}",
        )
    await session.commit()
    return len(rows)


async def expire_old_pending_payments(session: AsyncSession, days: int) -> int:
    """Expire `pending` payments (no receipt yet) older than `days`.

    Submitted receipts (awaiting admin), approved, rejected, and cancelled
    payments are never touched.
    """
    if days <= 0:
        return 0
    cutoff = _now() - timedelta(days=days)
    rows = list((await session.execute(
        select(Payment).where(Payment.status == "pending",
                              Payment.created_at < cutoff)
    )).scalars().all())
    now = _now()
    for payment in rows:
        payment.status = "expired"
        payment.expired_at = now
    if rows:
        await audit_service.log(
            session, actor_type="system", actor_id=None, action="payments_expired",
            target_type="payment", target_id=None, meta=f"count={len(rows)} days={days}",
        )
    await session.commit()
    return len(rows)


# --------------------------------------------------------------------------
# Admin listings (web)
# --------------------------------------------------------------------------
async def list_payments(
    session: AsyncSession, *, status: str | None = None,
    limit: int = 50, offset: int = 0,
) -> list[Payment]:
    stmt = select(Payment).order_by(Payment.id.desc()).limit(limit).offset(offset)
    if status:
        stmt = stmt.where(Payment.status == status)
    return list((await session.execute(stmt)).scalars().all())


def payment_meta(payment: Payment) -> dict:
    """Parse metadata_json safely (never raises)."""
    try:
        return json.loads(payment.metadata_json or "{}")
    except (TypeError, ValueError):
        return {}

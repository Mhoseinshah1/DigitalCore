"""Wallet: balances, card-to-card top-ups, wallet payment, and refunds (Phase 7).

Every balance change locks the user row (``SELECT … FOR UPDATE`` — a real lock on
Postgres, a no-op on SQLite's single writer), computes balance_before/after,
writes an immutable ``WalletTransaction``, and audits — inside one transaction.
Balances never go negative unless ``allow_negative_wallet`` is set (admin debits
only). Top-ups, purchases, and refunds are each idempotent: a second approve /
charge / refund on the same target fails or no-ops safely.

Top-up receipts reuse the order-receipt validation + on-disk storage
(``storage/receipts/wallet/YYYY/MM/<topup_id>_<name>``); bytes never hit the DB.
"""
from __future__ import annotations

import logging
import re
from datetime import datetime, timezone

from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.settings_service import SettingsService
from app.models.audit_log import AuditLog
from app.models.payment import Payment
from app.models.user import User
from app.models.wallet_topup import WalletTopupRequest
from app.models.wallet_transaction import WalletTransaction
from app.services import audit_service, order_service, payment_service

log = logging.getLogger("wallet")


class WalletError(ValueError):
    code = "wallet_error"

    def __init__(self, message: str, code: str | None = None) -> None:
        super().__init__(message)
        if code is not None:
            self.code = code


class InsufficientBalanceError(WalletError):
    def __init__(self, message: str = "insufficient wallet balance") -> None:
        super().__init__(message, code="insufficient_balance")


def _now() -> datetime:
    return datetime.now(timezone.utc)


# --------------------------------------------------------------------------
# Settings
# --------------------------------------------------------------------------
async def _allow_negative(session: AsyncSession) -> bool:
    return await SettingsService(session).get_bool("allow_negative_wallet", False)


async def wallet_enabled(session: AsyncSession) -> bool:
    return await SettingsService(session).get_bool("wallet_enabled", True)


async def topup_enabled(session: AsyncSession) -> bool:
    svc = SettingsService(session)
    return await svc.get_bool("wallet_enabled", True) and await svc.get_bool(
        "wallet_topup_enabled", True)


async def payment_enabled(session: AsyncSession) -> bool:
    svc = SettingsService(session)
    return await svc.get_bool("wallet_enabled", True) and await svc.get_bool(
        "wallet_payment_enabled", True)


# --------------------------------------------------------------------------
# Balance queries
# --------------------------------------------------------------------------
async def get_balance(session: AsyncSession, user_id: int) -> int:
    bal = await session.scalar(select(User.wallet_balance).where(User.id == user_id))
    return int(bal or 0)


async def list_transactions(
    session: AsyncSession, user_id: int, *, limit: int = 20, offset: int = 0
) -> list[WalletTransaction]:
    stmt = (select(WalletTransaction).where(WalletTransaction.user_id == user_id)
            .order_by(WalletTransaction.id.desc()).limit(limit).offset(offset))
    return list((await session.execute(stmt)).scalars().all())


# --------------------------------------------------------------------------
# Locked balance primitive
# --------------------------------------------------------------------------
async def _lock_user(session: AsyncSession, user_id: int) -> User:
    user = await session.scalar(
        select(User).where(User.id == user_id).with_for_update()
    )
    if user is None:
        raise WalletError("user not found", code="user_not_found")
    return user


def _audit_nocommit(
    session: AsyncSession, action: str, *, actor_type: str, actor_id: int | None,
    target_type: str, target_id: object, meta: str | None = None,
) -> None:
    """Add an audit row WITHOUT committing (keeps a held row lock in place)."""
    session.add(AuditLog(
        actor_type=actor_type, actor_id=actor_id, action=action, target_type=target_type,
        target_id=None if target_id is None else str(target_id),
        meta=None if meta is None else str(meta),
    ))


def _record_tx(
    session: AsyncSession, user: User, amount: int, *, tx_type: str, actor_type: str,
    actor_id: int | None = None, reason: str | None = None, order_id: int | None = None,
    payment_id: int | None = None, topup_id: int | None = None, allow_negative: bool = False,
) -> WalletTransaction:
    """Apply a signed balance change to an ALREADY-LOCKED user + ledger row.

    Raises InsufficientBalanceError if the result would go negative and
    `allow_negative` is False. Does not commit (the caller owns the transaction).
    """
    amount = int(amount)
    old = int(user.wallet_balance or 0)
    new = old + amount
    if new < 0 and not allow_negative:
        raise InsufficientBalanceError()
    user.wallet_balance = new
    tx = WalletTransaction(
        user_id=user.id, amount=amount, balance_before=old, balance_after=new,
        type=tx_type, status="completed", reason=(reason or None), order_id=order_id,
        payment_id=payment_id, topup_id=topup_id, actor_type=actor_type, actor_id=actor_id,
    )
    session.add(tx)
    return tx


# --------------------------------------------------------------------------
# Admin add / subtract (Phase 4 quick actions — now row-locked + linkable)
# --------------------------------------------------------------------------
async def add_balance(
    session: AsyncSession, user_id: int, amount: int, *, admin_id: int | None = None,
    reason: str | None = None, order_id: int | None = None, payment_id: int | None = None,
    transaction_type: str = "admin_adjustment", ip_address: str | None = None,
) -> User:
    if int(amount) <= 0:
        raise WalletError("amount must be a positive number", code="bad_amount")
    if transaction_type == "admin_adjustment" and not (reason or "").strip():
        raise WalletError("a reason is required", code="reason_required")
    user = await _lock_user(session, user_id)
    old = int(user.wallet_balance or 0)
    _record_tx(session, user, int(amount), tx_type=transaction_type, actor_type="admin",
               actor_id=admin_id, reason=reason, order_id=order_id, payment_id=payment_id)
    await session.flush()
    await audit_service.log(
        session, actor_type="admin", actor_id=admin_id, action="wallet_balance_added",
        target_type="user", target_id=user.id, old=str(old), new=str(user.wallet_balance),
        meta=f"amount={amount} reason={reason or ''}", ip_address=ip_address,
    )
    await session.refresh(user)
    return user


async def subtract_balance(
    session: AsyncSession, user_id: int, amount: int, *, admin_id: int | None = None,
    reason: str | None = None, order_id: int | None = None, payment_id: int | None = None,
    transaction_type: str = "admin_adjustment", ip_address: str | None = None,
) -> User:
    if int(amount) <= 0:
        raise WalletError("amount must be a positive number", code="bad_amount")
    if transaction_type == "admin_adjustment" and not (reason or "").strip():
        raise WalletError("a reason is required", code="reason_required")
    user = await _lock_user(session, user_id)
    old = int(user.wallet_balance or 0)
    _record_tx(session, user, -int(amount), tx_type=transaction_type, actor_type="admin",
               actor_id=admin_id, reason=reason, order_id=order_id, payment_id=payment_id,
               allow_negative=await _allow_negative(session))
    await session.flush()
    await audit_service.log(
        session, actor_type="admin", actor_id=admin_id, action="wallet_balance_subtracted",
        target_type="user", target_id=user.id, old=str(old), new=str(user.wallet_balance),
        meta=f"amount={amount} reason={reason or ''}", ip_address=ip_address,
    )
    await session.refresh(user)
    return user


# --------------------------------------------------------------------------
# Top-up requests
# --------------------------------------------------------------------------
async def get_topup(session: AsyncSession, topup_id: int) -> WalletTopupRequest | None:
    return await session.get(WalletTopupRequest, topup_id)


async def list_pending_topups(
    session: AsyncSession, *, limit: int = 50, offset: int = 0
) -> list[WalletTopupRequest]:
    stmt = (select(WalletTopupRequest).where(WalletTopupRequest.status == "waiting_admin")
            .order_by(WalletTopupRequest.id.desc()).limit(limit).offset(offset))
    return list((await session.execute(stmt)).scalars().all())


async def list_topups(
    session: AsyncSession, *, status: str | None = None, limit: int = 100, offset: int = 0
) -> list[WalletTopupRequest]:
    stmt = select(WalletTopupRequest)
    if status:
        stmt = stmt.where(WalletTopupRequest.status == status)
    stmt = stmt.order_by(WalletTopupRequest.id.desc()).limit(limit).offset(offset)
    return list((await session.execute(stmt)).scalars().all())


async def latest_pending_receipt_topup(
    session: AsyncSession, user_id: int
) -> WalletTopupRequest | None:
    """The user's most recent top-up still awaiting a receipt."""
    stmt = (select(WalletTopupRequest)
            .where(WalletTopupRequest.user_id == user_id,
                   WalletTopupRequest.status == "pending_receipt")
            .order_by(WalletTopupRequest.id.desc()).limit(1))
    return await session.scalar(stmt)


async def create_topup_request(
    session: AsyncSession, user_id: int, amount: int
) -> WalletTopupRequest:
    """Validate the amount against the wallet settings and open a top-up request."""
    if not await topup_enabled(session):
        raise WalletError("wallet top-up is disabled", code="topup_disabled")
    try:
        amount = int(amount)
    except (TypeError, ValueError):
        raise WalletError("amount must be a whole number", code="bad_amount") from None
    if amount <= 0:
        raise WalletError("amount must be a positive number", code="bad_amount")
    svc = SettingsService(session)
    min_topup = await svc.get_int("min_wallet_topup", 0)
    max_topup = await svc.get_int("max_wallet_topup", 0)
    if min_topup and amount < min_topup:
        raise WalletError(f"minimum top-up is {min_topup}", code="below_min")
    if max_topup and amount > max_topup:
        raise WalletError(f"maximum top-up is {max_topup}", code="above_max")

    topup = WalletTopupRequest(user_id=user_id, amount=amount, status="pending_receipt")
    session.add(topup)
    await session.flush()
    await audit_service.log(
        session, actor_type="user", actor_id=user_id, action="wallet_topup_created",
        target_type="wallet_topup", target_id=topup.id, meta=f"amount={amount}",
    )
    await session.refresh(topup)
    return topup


def _wallet_receipt_relpath(topup_id: int, original_name: str, mime_type: str | None,
                            when: datetime) -> str:
    ext = payment_service._ext_of(original_name, mime_type) or "bin"
    safe_name = payment_service._sanitize_filename(original_name, fallback_ext=ext)
    return f"wallet/{when.year:04d}/{when.month:02d}/{int(topup_id)}_{safe_name}"


async def submit_topup_receipt(
    session: AsyncSession, topup_id: int, user_id: int, file_info: payment_service.ReceiptFile
) -> WalletTopupRequest:
    """Store a top-up receipt and move the request to waiting_admin."""
    topup = await get_topup(session, topup_id)
    if topup is None:
        raise WalletError("top-up not found", code="topup_not_found")
    if topup.user_id != user_id:
        raise WalletError("not your top-up", code="not_your_topup")
    if topup.status != "pending_receipt":
        raise WalletError("a receipt was already submitted", code="already_submitted")

    ext = payment_service.validate_receipt(
        file_info.original_name, file_info.size, file_info.mime_type)
    when = _now()
    rel = _wallet_receipt_relpath(topup_id, file_info.original_name, file_info.mime_type, when)
    dest = payment_service.RECEIPTS_ROOT / rel
    dest.parent.mkdir(parents=True, exist_ok=True)
    dest.write_bytes(file_info.content)

    topup.receipt_path = rel
    topup.receipt_file_id = file_info.file_id
    topup.receipt_mime_type = file_info.mime_type or payment_service._MIME_FOR_EXT.get(ext)
    topup.receipt_original_name = file_info.original_name
    topup.receipt_size = file_info.size
    topup.status = "waiting_admin"
    topup.submitted_at = when
    await audit_service.log(
        session, actor_type="user", actor_id=user_id,
        action="wallet_topup_receipt_submitted", target_type="wallet_topup",
        target_id=topup.id, meta=f"amount={topup.amount} size={file_info.size}",
    )
    await session.refresh(topup)
    return topup


async def approve_topup(
    session: AsyncSession, topup_id: int, admin_id: int | None, *, bot=None
) -> dict:
    """Credit the user's wallet for a submitted top-up. Idempotent (only from
    waiting_admin) so a second approve fails safely."""
    # Lock the top-up row first and re-check its status under the lock so two
    # concurrent approvals cannot both credit the wallet.
    topup = await session.scalar(
        select(WalletTopupRequest).where(WalletTopupRequest.id == topup_id).with_for_update()
    )
    if topup is None:
        raise WalletError("top-up not found", code="topup_not_found")
    if topup.status != "waiting_admin":
        raise WalletError("top-up is not awaiting review", code="not_reviewable")

    user = await _lock_user(session, topup.user_id)
    _record_tx(session, user, int(topup.amount), tx_type="deposit", actor_type="admin",
               actor_id=admin_id, reason="wallet top-up", topup_id=topup.id)
    topup.status = "approved"
    topup.admin_id = admin_id
    topup.approved_at = _now()
    await audit_service.log(
        session, actor_type="admin", actor_id=admin_id, action="wallet_topup_approved",
        target_type="wallet_topup", target_id=topup.id,
        meta=f"user_id={topup.user_id} amount={topup.amount} balance_after={user.wallet_balance}",
    )
    await _notify(bot, user, "wallet.notify.topup_approved", amount=topup.amount,
                  balance=user.wallet_balance)
    return {"ok": True, "topup_id": topup.id, "balance": user.wallet_balance}


async def reject_topup(
    session: AsyncSession, topup_id: int, admin_id: int | None, reason: str, *, bot=None
) -> dict:
    """Reject a submitted top-up with a required reason (no balance change)."""
    if not (reason or "").strip():
        raise WalletError("a reason is required", code="reason_required")
    topup = await session.scalar(
        select(WalletTopupRequest).where(WalletTopupRequest.id == topup_id).with_for_update()
    )
    if topup is None:
        raise WalletError("top-up not found", code="topup_not_found")
    if topup.status != "waiting_admin":
        raise WalletError("top-up is not awaiting review", code="not_reviewable")
    topup.status = "rejected"
    topup.admin_id = admin_id
    topup.rejected_at = _now()
    topup.reject_reason = reason.strip()
    await audit_service.log(
        session, actor_type="admin", actor_id=admin_id, action="wallet_topup_rejected",
        target_type="wallet_topup", target_id=topup.id,
        meta=f"user_id={topup.user_id} amount={topup.amount} reason={reason.strip()}",
    )
    user = await session.get(User, topup.user_id)
    await _notify(bot, user, "wallet.notify.topup_rejected", amount=topup.amount,
                  reason=reason.strip())
    return {"ok": True, "topup_id": topup.id}


# --------------------------------------------------------------------------
# Wallet payment for an order
# --------------------------------------------------------------------------
async def pay_order_with_wallet(
    session: AsyncSession, order_id: int, user_id: int, *, actor_id: int | None = None, bot=None
) -> dict:
    """Charge the wallet for an order, approve it, and run delivery. Idempotent:
    a second call for an already-paid order does not charge again."""
    if not await payment_enabled(session):
        raise WalletError("wallet payment is disabled", code="payment_disabled")

    # Lock the buyer's wallet row for the whole check-and-charge (serialises
    # concurrent pays for the same user; no intermediate commit until the audit).
    user = await _lock_user(session, user_id)
    order = await order_service.get_order(session, order_id)
    if order is None:
        raise WalletError("order not found", code="order_not_found")
    if order.user_id != user_id:
        raise WalletError("not your order", code="not_your_order")
    payment = await payment_service.get_payment_by_order(session, order_id)

    # Already paid/approved → do not charge again.
    if order.status != "pending_payment" or (payment is not None and payment.status != "pending"):
        return {"ok": True, "already": True, "order_id": order.id,
                "charged": False, "balance": int(user.wallet_balance or 0)}

    amount = int(order.final_amount or 0)
    balance = int(user.wallet_balance or 0)
    if balance < amount:
        # Terminal: record the attempt and abort (no balance change).
        await audit_service.log(
            session, actor_type="user", actor_id=user_id, action="wallet_insufficient_balance",
            target_type="order", target_id=order.id,
            meta=f"required={amount} balance={balance}",
        )
        raise InsufficientBalanceError(
            f"balance {balance} is below the required {amount}")

    # Everything below runs under the still-held user-row lock with NO intermediate
    # commit (audit rows are added, not committed) so the check-and-charge is atomic
    # and a concurrent pay for the same order sees the committed result and no-ops.
    if payment is None:
        payment = Payment(order_id=order.id, user_id=order.user_id, amount=amount,
                          method="wallet", status="pending")
        session.add(payment)
        await session.flush()
    payment.method = "wallet"

    _audit_nocommit(session, "wallet_payment_started", actor_type="user", actor_id=user_id,
                    target_type="order", target_id=order.id, meta=f"amount={amount}")
    _record_tx(session, user, -amount, tx_type="purchase", actor_type="user",
               actor_id=user_id, reason=f"order {order.order_number}",
               order_id=order.id, payment_id=payment.id)

    now = _now()
    payment.status = "approved"
    payment.approved_at = now
    order.status = "approved"
    order.paid_at = now
    order.approved_at = now
    _audit_nocommit(session, "wallet_payment_completed", actor_type="user", actor_id=user_id,
                    target_type="order", target_id=order.id,
                    meta=f"amount={amount} balance_after={user.wallet_balance} payment_id={payment.id}")
    # Commit the charge atomically (balance + order + payment + audits), which
    # releases the user-row lock, BEFORE running delivery.
    await session.commit()

    # Consume the coupon now that the order is paid (idempotent + race-safe).
    from app.services import coupon_service, delivery_service, referral_service
    try:
        if await coupon_service.record_usage(session, order.id):
            await session.commit()
    except Exception as exc:  # noqa: BLE001 - coupon accounting never blocks delivery
        log.warning("coupon usage record failed for order %s: %s", order.id, exc)

    # Reuse the existing delivery dispatcher (license sell / v2ray provision).
    delivery = await delivery_service.deliver_order(session, order, actor_id=actor_id, bot=bot)
    await session.commit()

    # Mint a referral reward for a qualifying first paid order (idempotent).
    try:
        await referral_service.create_reward_for_order(session, order.id, bot=bot)
    except Exception as exc:  # noqa: BLE001 - reward creation never blocks the purchase
        log.warning("referral reward creation failed for order %s: %s", order.id, exc)

    await session.refresh(order)
    return {"ok": True, "already": False, "charged": True, "order_id": order.id,
            "amount": amount, "balance": int(user.wallet_balance or 0), "delivery": delivery}


# --------------------------------------------------------------------------
# Refund
# --------------------------------------------------------------------------
async def refund_wallet_payment(
    session: AsyncSession, order_id: int, *, admin_id: int | None = None,
    reason: str | None = None, bot=None
) -> dict:
    """Refund an order's charge back to the buyer's wallet. Idempotent — a second
    refund on the same order does nothing."""
    if not (reason or "").strip():
        raise WalletError("a reason is required", code="reason_required")
    # Lock the order row and re-check under the lock so two concurrent refunds
    # cannot both credit the wallet.
    from app.models.order import Order
    await session.execute(select(Order.id).where(Order.id == order_id).with_for_update())
    order = await order_service.get_order(session, order_id)
    if order is None:
        raise WalletError("order not found", code="order_not_found")
    if order.refunded_at is not None:
        raise WalletError("order was already refunded", code="already_refunded")
    if order.status not in ("approved", "provisioning_pending", "delivered", "failed"):
        raise WalletError("order is not in a refundable state", code="not_refundable")

    payment = await payment_service.get_payment_by_order(session, order_id)
    amount = int((payment.amount if payment else order.final_amount) or 0)
    if amount <= 0:
        raise WalletError("nothing to refund", code="nothing_to_refund")

    user = await _lock_user(session, order.user_id)
    _record_tx(session, user, amount, tx_type="refund", actor_type="admin",
               actor_id=admin_id, reason=reason.strip(), order_id=order.id,
               payment_id=(payment.id if payment else None))
    now = _now()
    order.refunded_at = now
    order.refund_reason = reason.strip()
    if payment is not None:
        payment.refunded_at = now
        payment.refunded_amount = amount
    await audit_service.log(
        session, actor_type="admin", actor_id=admin_id, action="wallet_refund_created",
        target_type="order", target_id=order.id,
        meta=f"user_id={order.user_id} amount={amount} balance_after={user.wallet_balance} reason={reason.strip()}",
    )
    await _notify(bot, user, "wallet.notify.refund", amount=amount,
                  balance=user.wallet_balance)
    return {"ok": True, "order_id": order.id, "amount": amount,
            "balance": int(user.wallet_balance or 0)}


# --------------------------------------------------------------------------
# Best-effort user notification
# --------------------------------------------------------------------------
async def _notify(bot, user: User | None, key: str, **params) -> None:
    if user is None or not user.telegram_id:
        return
    from app.i18n import t
    lang = user.language if user and user.language else "fa"
    text = t(key, lang, **params)
    b, own = bot, None
    if b is None:
        from app.config import settings
        if not settings.TELEGRAM_BOT_TOKEN:
            return
        from aiogram import Bot
        own = b = Bot(settings.TELEGRAM_BOT_TOKEN)
    try:
        await b.send_message(user.telegram_id, text, parse_mode="HTML")
    except Exception as exc:  # noqa: BLE001 - notification is best-effort
        log.info("wallet notify failed: %s", exc)
    finally:
        if own is not None:
            try:
                await own.session.close()
            except Exception:  # noqa: BLE001
                pass

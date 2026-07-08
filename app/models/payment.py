"""Payments: the money side of an Order.

Phase 3 implements card-to-card only, and only the pending -> receipt_submitted
transitions. Receipt files live on disk (see payment_service); only their
metadata + a repo-relative path are stored here — never the bytes.
"""
from __future__ import annotations

from datetime import datetime
from typing import TYPE_CHECKING

from sqlalchemy import BigInteger, DateTime, ForeignKey, Integer, String, Text
from sqlalchemy.orm import Mapped, mapped_column, relationship

from app.models.base import Base, TimestampMixin

if TYPE_CHECKING:
    from app.models.order import Order

# `approved` is the platform's "paid" state (kept for backward compatibility);
# expired/cancelled were added by the Payment Core slice for cleanup flows.
PAYMENT_STATUSES: tuple[str, ...] = (
    "pending",
    "receipt_submitted",
    "approved",
    "rejected",
    "failed",
    "expired",
    "cancelled",
)

# What the payment is FOR (mirrors Invoice.invoice_type).
PAYMENT_TYPES: tuple[str, ...] = (
    "product_purchase", "wallet_topup", "renewal", "add_traffic", "add_time",
)


class Payment(Base, TimestampMixin):
    __tablename__ = "payments"

    id: Mapped[int] = mapped_column(primary_key=True)
    # Nullable since the Payment Core slice: a wallet top-up payment has no order.
    order_id: Mapped[int | None] = mapped_column(
        ForeignKey("orders.id", ondelete="CASCADE"), nullable=True, index=True
    )
    invoice_id: Mapped[int | None] = mapped_column(
        ForeignKey("invoices.id", ondelete="SET NULL"), nullable=True, index=True
    )
    user_id: Mapped[int] = mapped_column(
        ForeignKey("users.id", ondelete="CASCADE"), nullable=False, index=True
    )

    amount: Mapped[int] = mapped_column(BigInteger, nullable=False, default=0)
    # Gateway cashback credited on top of `amount` for wallet top-ups.
    bonus_amount: Mapped[int] = mapped_column(
        BigInteger, nullable=False, default=0, server_default="0"
    )
    final_wallet_credit: Mapped[int | None] = mapped_column(BigInteger, nullable=True)
    payment_type: Mapped[str] = mapped_column(
        String(20), nullable=False, default="product_purchase",
        server_default="product_purchase",
    )
    method: Mapped[str] = mapped_column(String(16), nullable=False, default="card_to_card")
    status: Mapped[str] = mapped_column(String(32), nullable=False, default="pending", index=True)

    # Unique payment tracking code (PAY-…), nullable for pre-slice rows.
    tracking_code: Mapped[str | None] = mapped_column(
        String(120), nullable=True, unique=True, index=True
    )

    # Gateway plumbing (future phases): which provider handled this payment and
    # its external reference / redirect URL. Never credentials.
    provider_name: Mapped[str | None] = mapped_column(String(60), nullable=True)
    provider_payment_id: Mapped[str | None] = mapped_column(String(120), nullable=True)
    provider_url: Mapped[str | None] = mapped_column(String(512), nullable=True)

    reject_reason: Mapped[str | None] = mapped_column(Text, nullable=True)
    metadata_json: Mapped[str | None] = mapped_column(Text, nullable=True)

    # Receipt: the Telegram file_id (for re-sending), a repo-relative on-disk path,
    # and the metadata used to serve it safely and to validate on upload.
    receipt_file_id: Mapped[str | None] = mapped_column(String(255), nullable=True)
    receipt_path: Mapped[str | None] = mapped_column(String(512), nullable=True)
    receipt_mime_type: Mapped[str | None] = mapped_column(String(128), nullable=True)
    receipt_original_name: Mapped[str | None] = mapped_column(String(255), nullable=True)
    receipt_size: Mapped[int | None] = mapped_column(Integer, nullable=True)

    admin_id: Mapped[int | None] = mapped_column(
        ForeignKey("admins.id", ondelete="SET NULL"), nullable=True
    )

    submitted_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    approved_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    rejected_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    expired_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)

    # Refund foundation (Phase 7). `refunded_amount` is 0 until refunded.
    refunded_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    refunded_amount: Mapped[int] = mapped_column(
        BigInteger, nullable=False, default=0, server_default="0"
    )

    order: Mapped["Order | None"] = relationship("Order", back_populates="payment")

    def __repr__(self) -> str:  # pragma: no cover - debug helper
        return f"<Payment id={self.id} order_id={self.order_id} status={self.status}>"

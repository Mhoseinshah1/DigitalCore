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

PAYMENT_STATUSES: tuple[str, ...] = (
    "pending",
    "receipt_submitted",
    "approved",
    "rejected",
    "failed",
)


class Payment(Base, TimestampMixin):
    __tablename__ = "payments"

    id: Mapped[int] = mapped_column(primary_key=True)
    order_id: Mapped[int] = mapped_column(
        ForeignKey("orders.id", ondelete="CASCADE"), nullable=False, index=True
    )
    user_id: Mapped[int] = mapped_column(
        ForeignKey("users.id", ondelete="CASCADE"), nullable=False, index=True
    )

    amount: Mapped[int] = mapped_column(BigInteger, nullable=False, default=0)
    method: Mapped[str] = mapped_column(String(16), nullable=False, default="card_to_card")
    status: Mapped[str] = mapped_column(String(32), nullable=False, default="pending", index=True)

    tracking_code: Mapped[str | None] = mapped_column(String(120), nullable=True)

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

    order: Mapped["Order"] = relationship("Order", back_populates="payment")

    def __repr__(self) -> str:  # pragma: no cover - debug helper
        return f"<Payment id={self.id} order_id={self.order_id} status={self.status}>"

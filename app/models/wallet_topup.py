"""Wallet top-up requests (Phase 7): a user's card-to-card wallet charge.

Kept separate from `Payment` (which is always tied to an order) so order money
and wallet money never blur. The flow mirrors the order receipt flow:
`pending_receipt → waiting_admin → approved | rejected`. Receipt bytes live on
disk (see wallet_service); only metadata + a receipts-root-relative path are
stored here.
"""
from __future__ import annotations

from datetime import datetime
from typing import TYPE_CHECKING

from sqlalchemy import BigInteger, DateTime, ForeignKey, Integer, String, Text
from sqlalchemy.orm import Mapped, mapped_column, relationship

from app.models.base import Base, TimestampMixin

if TYPE_CHECKING:
    from app.models.user import User

TOPUP_STATUSES: tuple[str, ...] = (
    "pending_receipt",  # created, awaiting the user's receipt upload
    "waiting_admin",    # receipt submitted, awaiting admin review
    "approved",         # credited to the wallet
    "rejected",         # refused with a reason
    "cancelled",
    "failed",
)


class WalletTopupRequest(Base, TimestampMixin):
    __tablename__ = "wallet_topup_requests"

    id: Mapped[int] = mapped_column(primary_key=True)
    user_id: Mapped[int] = mapped_column(
        ForeignKey("users.id", ondelete="CASCADE"), nullable=False, index=True
    )

    # Integer toman.
    amount: Mapped[int] = mapped_column(BigInteger, nullable=False, default=0)
    status: Mapped[str] = mapped_column(
        String(20), nullable=False, default="pending_receipt", index=True
    )

    # Receipt (same shape as Payment): Telegram file_id, a repo-relative on-disk
    # path, and metadata used to serve it safely and validate on upload.
    receipt_file_id: Mapped[str | None] = mapped_column(String(255), nullable=True)
    receipt_path: Mapped[str | None] = mapped_column(String(512), nullable=True)
    receipt_mime_type: Mapped[str | None] = mapped_column(String(128), nullable=True)
    receipt_original_name: Mapped[str | None] = mapped_column(String(255), nullable=True)
    receipt_size: Mapped[int | None] = mapped_column(Integer, nullable=True)

    admin_id: Mapped[int | None] = mapped_column(
        ForeignKey("admins.id", ondelete="SET NULL"), nullable=True
    )
    reject_reason: Mapped[str | None] = mapped_column(Text, nullable=True)

    submitted_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    approved_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    rejected_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)

    user: Mapped["User"] = relationship("User", lazy="selectin")

    def __repr__(self) -> str:  # pragma: no cover - debug helper
        return (f"<WalletTopupRequest id={self.id} user_id={self.user_id} "
                f"amount={self.amount} status={self.status}>")

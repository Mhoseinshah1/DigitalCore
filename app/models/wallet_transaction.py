"""Wallet transactions: an immutable ledger of every wallet balance change.

`amount` is signed integer toman — positive credits, negative debits — and
`balance_after` snapshots the resulting balance for a cheap audit trail. Phase 7
adds purchase/refund/deposit flows, so a transaction can now link back to the
order / payment / top-up that caused it and carries a `status` (`completed` for
a settled balance change). `reason` doubles as the human-readable description.
"""
from __future__ import annotations

from datetime import datetime

from sqlalchemy import BigInteger, DateTime, ForeignKey, Integer, String, func
from sqlalchemy.orm import Mapped, mapped_column

from app.models.base import Base

# Where the change originated.
WALLET_ACTOR_TYPES: tuple[str, ...] = ("admin", "system", "user")

# The full transaction vocabulary (Phase 7).
WALLET_TX_TYPES: tuple[str, ...] = (
    "deposit", "withdraw", "purchase", "refund", "reward",
    "admin_adjustment", "topup_pending", "topup_approved", "topup_rejected",
)

WALLET_TX_STATUSES: tuple[str, ...] = (
    "pending", "completed", "rejected", "failed", "cancelled",
)


class WalletTransaction(Base):
    __tablename__ = "wallet_transactions"

    id: Mapped[int] = mapped_column(primary_key=True)
    user_id: Mapped[int] = mapped_column(
        ForeignKey("users.id", ondelete="CASCADE"), index=True, nullable=False
    )

    # Signed integer toman: + credit, - debit.
    amount: Mapped[int] = mapped_column(BigInteger, nullable=False)
    balance_before: Mapped[int] = mapped_column(
        BigInteger, default=0, server_default="0", nullable=False
    )
    balance_after: Mapped[int] = mapped_column(BigInteger, nullable=False)

    # One of WALLET_TX_TYPES (default admin_adjustment for legacy rows).
    type: Mapped[str] = mapped_column(
        String(32), default="admin_adjustment", server_default="admin_adjustment",
        nullable=False,
    )
    # Settled ledger rows are `completed`; the column exists for future
    # pending/failed accounting.
    status: Mapped[str] = mapped_column(
        String(16), default="completed", server_default="completed", nullable=False
    )

    # Human-readable description of the change.
    reason: Mapped[str | None] = mapped_column(String(255), nullable=True)

    # What caused the change (any may be null).
    order_id: Mapped[int | None] = mapped_column(
        ForeignKey("orders.id", ondelete="SET NULL"), nullable=True, index=True
    )
    payment_id: Mapped[int | None] = mapped_column(
        ForeignKey("payments.id", ondelete="SET NULL"), nullable=True
    )
    topup_id: Mapped[int | None] = mapped_column(
        ForeignKey("wallet_topup_requests.id", ondelete="SET NULL"), nullable=True
    )

    # `actor_id` is the acting admin's id for admin adjustments/top-ups.
    actor_type: Mapped[str] = mapped_column(String(16), default="admin", nullable=False)
    actor_id: Mapped[int | None] = mapped_column(BigInteger, nullable=True)

    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), nullable=False, index=True
    )

    def __repr__(self) -> str:  # pragma: no cover - debug helper
        return (
            f"<WalletTransaction id={self.id} user_id={self.user_id} "
            f"amount={self.amount} balance_after={self.balance_after}>"
        )

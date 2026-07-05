"""Wallet transactions: an immutable ledger of every wallet balance change.

Phase 2 only records admin/system adjustments (no purchase logic yet). `amount`
is signed integer toman — positive credits, negative debits — and
`balance_after` snapshots the resulting balance for a cheap audit trail.
"""
from __future__ import annotations

from datetime import datetime

from sqlalchemy import BigInteger, DateTime, ForeignKey, String, func
from sqlalchemy.orm import Mapped, mapped_column

from app.models.base import Base

# Where the change originated.
WALLET_ACTOR_TYPES: tuple[str, ...] = ("admin", "system", "user")


class WalletTransaction(Base):
    __tablename__ = "wallet_transactions"

    id: Mapped[int] = mapped_column(primary_key=True)
    user_id: Mapped[int] = mapped_column(
        ForeignKey("users.id", ondelete="CASCADE"), index=True, nullable=False
    )

    # Signed integer toman: + credit, - debit.
    amount: Mapped[int] = mapped_column(BigInteger, nullable=False)
    balance_after: Mapped[int] = mapped_column(BigInteger, nullable=False)

    reason: Mapped[str | None] = mapped_column(String(255), nullable=True)

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

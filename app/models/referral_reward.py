"""Referral rewards (Phase 10).

A reward is created for the referrer when a referred user completes a qualifying
paid order (see referral_service). Auto rewards land `paid` (wallet credited);
approval-required rewards start `pending`. `order_id` is unique so one order can
never mint two rewards; the referred-user first-order rule is enforced in the
service under a row lock.
"""
from __future__ import annotations

from datetime import datetime
from typing import TYPE_CHECKING

from sqlalchemy import BigInteger, DateTime, ForeignKey, String, Text, UniqueConstraint
from sqlalchemy.orm import Mapped, mapped_column, relationship

from app.models.base import Base, TimestampMixin

if TYPE_CHECKING:
    from app.models.user import User

REFERRAL_REWARD_TYPES: tuple[str, ...] = ("fixed", "percent")
REFERRAL_REWARD_STATUSES: tuple[str, ...] = (
    "pending",   # awaiting admin approval
    "approved",  # approved, not yet paid
    "rejected",  # refused
    "paid",      # credited to the referrer's wallet
    "cancelled",
)


class ReferralReward(Base, TimestampMixin):
    __tablename__ = "referral_rewards"
    __table_args__ = (
        # One order yields at most one reward (idempotency backstop).
        UniqueConstraint("order_id", name="uq_referral_rewards_order"),
    )

    id: Mapped[int] = mapped_column(primary_key=True)
    referrer_user_id: Mapped[int] = mapped_column(
        ForeignKey("users.id", ondelete="CASCADE"), nullable=False, index=True
    )
    referred_user_id: Mapped[int] = mapped_column(
        ForeignKey("users.id", ondelete="CASCADE"), nullable=False, index=True
    )
    order_id: Mapped[int | None] = mapped_column(
        ForeignKey("orders.id", ondelete="SET NULL"), nullable=True, index=True
    )

    reward_type: Mapped[str] = mapped_column(String(10), nullable=False, default="fixed")
    reward_amount: Mapped[int] = mapped_column(BigInteger, nullable=False, default=0)

    status: Mapped[str] = mapped_column(
        String(12), nullable=False, default="pending", index=True
    )
    reject_reason: Mapped[str | None] = mapped_column(Text, nullable=True)

    approved_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    paid_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    cancelled_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)

    referrer: Mapped["User"] = relationship(
        "User", foreign_keys=[referrer_user_id], lazy="selectin"
    )
    referred: Mapped["User"] = relationship(
        "User", foreign_keys=[referred_user_id], lazy="selectin"
    )

    def __repr__(self) -> str:  # pragma: no cover - debug helper
        return (f"<ReferralReward id={self.id} referrer={self.referrer_user_id} "
                f"amount={self.reward_amount} status={self.status}>")

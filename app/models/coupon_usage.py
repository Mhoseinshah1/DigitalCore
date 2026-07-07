"""One consumption of a coupon on an order (Phase 10).

Written when a coupon is actually consumed (order paid). The
``(coupon_id, order_id)`` unique constraint makes consumption idempotent — a
retry never records a second usage for the same order — while per-user limits are
enforced in coupon_service by counting these rows.
"""
from __future__ import annotations

from typing import TYPE_CHECKING

from sqlalchemy import BigInteger, ForeignKey, UniqueConstraint
from sqlalchemy.orm import Mapped, mapped_column, relationship

from app.models.base import Base, TimestampMixin

if TYPE_CHECKING:
    from app.models.coupon import Coupon


class CouponUsage(Base, TimestampMixin):
    __tablename__ = "coupon_usages"
    __table_args__ = (
        UniqueConstraint("coupon_id", "order_id", name="uq_coupon_usages_coupon_order"),
    )

    id: Mapped[int] = mapped_column(primary_key=True)
    coupon_id: Mapped[int] = mapped_column(
        ForeignKey("coupons.id", ondelete="CASCADE"), nullable=False, index=True
    )
    user_id: Mapped[int] = mapped_column(
        ForeignKey("users.id", ondelete="CASCADE"), nullable=False, index=True
    )
    order_id: Mapped[int] = mapped_column(
        ForeignKey("orders.id", ondelete="CASCADE"), nullable=False, index=True
    )
    discount_amount: Mapped[int] = mapped_column(BigInteger, nullable=False, default=0)

    coupon: Mapped["Coupon"] = relationship("Coupon", lazy="selectin")

    def __repr__(self) -> str:  # pragma: no cover - debug helper
        return (f"<CouponUsage coupon={self.coupon_id} order={self.order_id} "
                f"discount={self.discount_amount}>")

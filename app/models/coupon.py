"""Discount coupons (Phase 10).

A coupon applies a percent or fixed discount to an order, optionally restricted to
a product, a product type (license/v2ray), and/or an action (new_purchase /
renew_service / add_traffic). `code` is stored normalized (UPPERCASE, trimmed);
`used_count` is bumped race-safely under a row lock when a coupon is consumed on
payment (see coupon_service). Money is integer toman, matching the platform.
"""
from __future__ import annotations

from datetime import datetime
from typing import TYPE_CHECKING

from sqlalchemy import BigInteger, Boolean, DateTime, ForeignKey, Integer, String, Text
from sqlalchemy.orm import Mapped, mapped_column, relationship

from app.models.base import Base, TimestampMixin

if TYPE_CHECKING:
    from app.models.product import Product

COUPON_DISCOUNT_TYPES: tuple[str, ...] = ("percent", "fixed")
# Which product types a coupon may be limited to. "any" == no restriction.
COUPON_PRODUCT_TYPES: tuple[str, ...] = ("license", "v2ray", "any")
# Which purchase actions a coupon may be limited to. "any" == no restriction.
COUPON_ACTIONS: tuple[str, ...] = ("new_purchase", "renew_service", "add_traffic", "any")


class Coupon(Base, TimestampMixin):
    __tablename__ = "coupons"

    id: Mapped[int] = mapped_column(primary_key=True)
    code: Mapped[str] = mapped_column(String(64), nullable=False, unique=True, index=True)
    title: Mapped[str | None] = mapped_column(String(200), nullable=True)
    description: Mapped[str | None] = mapped_column(Text, nullable=True)

    discount_type: Mapped[str] = mapped_column(String(10), nullable=False, default="percent")
    # percent: 1..100 ; fixed: positive integer toman.
    discount_value: Mapped[int] = mapped_column(BigInteger, nullable=False, default=0)
    # Caps the discount for a percent coupon (toman). Null == uncapped.
    max_discount_amount: Mapped[int | None] = mapped_column(BigInteger, nullable=True)
    # The order (pre-discount) must be at least this to use the coupon. Null == 0.
    min_order_amount: Mapped[int | None] = mapped_column(BigInteger, nullable=True)

    usage_limit: Mapped[int | None] = mapped_column(Integer, nullable=True)
    usage_limit_per_user: Mapped[int | None] = mapped_column(Integer, nullable=True)
    used_count: Mapped[int] = mapped_column(Integer, nullable=False, default=0, server_default="0")

    starts_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    expires_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    is_active: Mapped[bool] = mapped_column(
        Boolean, nullable=False, default=True, server_default="1", index=True
    )

    # Restrictions (all optional). product_id pins one product; product_type limits
    # to license/v2ray; applies_to_action limits to a purchase action.
    product_id: Mapped[int | None] = mapped_column(
        ForeignKey("products.id", ondelete="SET NULL"), nullable=True, index=True
    )
    product_type: Mapped[str | None] = mapped_column(String(16), nullable=True)
    applies_to_action: Mapped[str | None] = mapped_column(String(20), nullable=True)

    created_by_admin_id: Mapped[int | None] = mapped_column(
        ForeignKey("admins.id", ondelete="SET NULL"), nullable=True
    )

    product: Mapped["Product | None"] = relationship("Product", lazy="selectin")

    def __repr__(self) -> str:  # pragma: no cover - debug helper
        return (f"<Coupon id={self.id} code={self.code!r} "
                f"{self.discount_type}={self.discount_value}>")

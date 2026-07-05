"""A simple per-product license-key pool consumed on license delivery (Phase 4).

Admins stock keys against a `license` product; approving a license order pops the
next unused key and marks it used. Nothing generates keys automatically.
"""
from __future__ import annotations

from datetime import datetime
from typing import TYPE_CHECKING

from sqlalchemy import Boolean, DateTime, ForeignKey, String, UniqueConstraint, func
from sqlalchemy.orm import Mapped, mapped_column, relationship

from app.models.base import Base

if TYPE_CHECKING:
    from app.models.product import Product


class LicenseKey(Base):
    __tablename__ = "license_keys"
    __table_args__ = (
        UniqueConstraint("product_id", "code", name="uq_license_keys_product_code"),
    )

    id: Mapped[int] = mapped_column(primary_key=True)
    product_id: Mapped[int] = mapped_column(
        ForeignKey("products.id", ondelete="CASCADE"), nullable=False, index=True
    )
    code: Mapped[str] = mapped_column(String(255), nullable=False)
    is_used: Mapped[bool] = mapped_column(
        Boolean, default=False, server_default="0", nullable=False, index=True
    )
    order_id: Mapped[int | None] = mapped_column(
        ForeignKey("orders.id", ondelete="SET NULL"), nullable=True
    )
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), nullable=False
    )
    used_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)

    product: Mapped["Product"] = relationship("Product", lazy="selectin")

    def __repr__(self) -> str:  # pragma: no cover - debug helper
        return f"<LicenseKey id={self.id} product={self.product_id} used={self.is_used}>"

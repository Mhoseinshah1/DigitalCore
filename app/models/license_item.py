"""License stock: one sellable license (an email/password credential pair).

Phase 5's real license model, replacing the Phase 4 code-pool. A license moves
available -> reserved -> sold during delivery; admins may mark it blocked/broken
or replace it. The password is stored (delivery needs it) but is never shown on
list pages, never logged, and only revealed on the detail page to admins with the
`view_license_secrets` permission.
"""
from __future__ import annotations

from datetime import datetime
from typing import TYPE_CHECKING

from sqlalchemy import (
    DateTime,
    ForeignKey,
    Integer,
    String,
    Text,
    UniqueConstraint,
)
from sqlalchemy.orm import Mapped, mapped_column, relationship

from app.models.base import Base, TimestampMixin

if TYPE_CHECKING:
    from app.models.product import Product
    from app.models.user import User

LICENSE_STATUSES: tuple[str, ...] = (
    "available",   # can be sold
    "reserved",    # locked during a delivery transaction
    "sold",        # delivered to a user
    "blocked",     # withheld — cannot be sold
    "broken",      # marked bad by admin/user
    "replaced",    # superseded by another license
)


class LicenseItem(Base, TimestampMixin):
    __tablename__ = "license_items"
    __table_args__ = (
        UniqueConstraint("product_id", "email", name="uq_license_items_product_email"),
    )

    id: Mapped[int] = mapped_column(primary_key=True)
    product_id: Mapped[int] = mapped_column(
        ForeignKey("products.id", ondelete="CASCADE"), nullable=False, index=True
    )

    email: Mapped[str] = mapped_column(String(255), nullable=False)
    password: Mapped[str] = mapped_column(String(255), nullable=False)
    note: Mapped[str | None] = mapped_column(Text, nullable=True)

    status: Mapped[str] = mapped_column(
        String(16), nullable=False, default="available", server_default="available",
        index=True,
    )

    sold_to_user_id: Mapped[int | None] = mapped_column(
        ForeignKey("users.id", ondelete="SET NULL"), nullable=True, index=True
    )
    order_id: Mapped[int | None] = mapped_column(
        ForeignKey("orders.id", ondelete="SET NULL"), nullable=True, index=True
    )

    reserved_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    sold_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)

    imported_by_admin_id: Mapped[int | None] = mapped_column(
        ForeignKey("admins.id", ondelete="SET NULL"), nullable=True
    )
    replaced_by_license_id: Mapped[int | None] = mapped_column(
        ForeignKey("license_items.id", ondelete="SET NULL"), nullable=True
    )
    admin_note: Mapped[str | None] = mapped_column(Text, nullable=True)

    product: Mapped["Product"] = relationship("Product", lazy="selectin")
    sold_to_user: Mapped["User | None"] = relationship("User", lazy="selectin")

    def __repr__(self) -> str:  # pragma: no cover - debug helper
        return f"<LicenseItem id={self.id} product={self.product_id} status={self.status}>"

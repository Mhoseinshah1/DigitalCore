"""Invoices: the financial document a user pays (Payment Core slice 1).

An Invoice is the user-facing bill — a pre-invoice shown in the bot becomes an
unpaid Invoice; paying it (wallet, manual receipt, or a future gateway) settles
it. It complements — never replaces — Order (what was bought) and Payment (how
the money moved): a product invoice links to its order, a wallet top-up invoice
stands alone. Amounts are integer toman, matching the platform convention.
"""
from __future__ import annotations

from datetime import datetime
from typing import TYPE_CHECKING

from sqlalchemy import BigInteger, DateTime, ForeignKey, String, Text
from sqlalchemy.orm import Mapped, mapped_column, relationship

from app.models.base import Base, TimestampMixin

if TYPE_CHECKING:
    from app.models.product import Product
    from app.models.user import User

INVOICE_TYPES: tuple[str, ...] = (
    "product_purchase", "wallet_topup", "renewal", "add_traffic", "add_time",
)

INVOICE_STATUSES: tuple[str, ...] = ("unpaid", "paid", "expired", "cancelled")


class Invoice(Base, TimestampMixin):
    __tablename__ = "invoices"

    id: Mapped[int] = mapped_column(primary_key=True)
    invoice_number: Mapped[str] = mapped_column(
        String(40), nullable=False, unique=True, index=True
    )
    tracking_code: Mapped[str] = mapped_column(
        String(40), nullable=False, unique=True, index=True
    )

    user_id: Mapped[int] = mapped_column(
        ForeignKey("users.id", ondelete="CASCADE"), nullable=False, index=True
    )
    order_id: Mapped[int | None] = mapped_column(
        ForeignKey("orders.id", ondelete="SET NULL"), nullable=True, index=True
    )
    product_id: Mapped[int | None] = mapped_column(
        ForeignKey("products.id", ondelete="SET NULL"), nullable=True, index=True
    )

    invoice_type: Mapped[str] = mapped_column(
        String(20), nullable=False, default="product_purchase", index=True
    )

    # Integer toman. final_amount = amount - discount_amount.
    amount: Mapped[int] = mapped_column(BigInteger, nullable=False, default=0)
    discount_amount: Mapped[int] = mapped_column(
        BigInteger, nullable=False, default=0, server_default="0"
    )
    final_amount: Mapped[int] = mapped_column(BigInteger, nullable=False, default=0)
    currency: Mapped[str] = mapped_column(
        String(10), nullable=False, default="toman", server_default="toman"
    )

    status: Mapped[str] = mapped_column(
        String(16), nullable=False, default="unpaid", index=True
    )

    # Free-form JSON string (e.g. {"topup_id": 3}); never secrets.
    metadata_json: Mapped[str | None] = mapped_column(Text, nullable=True)

    expires_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    paid_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    cancelled_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)

    user: Mapped["User"] = relationship("User", lazy="selectin")
    product: Mapped["Product | None"] = relationship("Product", lazy="selectin")

    def __repr__(self) -> str:  # pragma: no cover - debug helper
        return (f"<Invoice id={self.id} number={self.invoice_number!r} "
                f"type={self.invoice_type} status={self.status}>")

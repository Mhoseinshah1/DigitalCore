"""Orders: a user's request to buy one product.

Phase 3 only actively uses the pending_payment -> waiting_admin -> cancelled part
of the state machine; approved/rejected/failed/delivered exist so later phases
can extend the flow without another migration. Money is integer toman, matching
the Product convention.
"""
from __future__ import annotations

from datetime import datetime
from typing import TYPE_CHECKING

from sqlalchemy import BigInteger, ForeignKey, String, Text
from sqlalchemy import DateTime
from sqlalchemy.orm import Mapped, mapped_column, relationship

from app.models.base import Base, TimestampMixin

if TYPE_CHECKING:
    from app.models.payment import Payment
    from app.models.product import Product
    from app.models.user import User

# Full state machine (only the first three are exercised in Phase 3).
ORDER_STATUSES: tuple[str, ...] = (
    "pending_payment",
    "waiting_admin",
    "approved",
    "provisioning_pending",  # approved, awaiting V2Ray provisioning (Phase 6)
    "rejected",
    "cancelled",
    "failed",
    "delivered",
)

# Only card_to_card is implemented in Phase 3.
PAYMENT_METHODS: tuple[str, ...] = ("card_to_card", "wallet", "gateway")


class Order(Base, TimestampMixin):
    __tablename__ = "orders"

    id: Mapped[int] = mapped_column(primary_key=True)
    order_number: Mapped[str] = mapped_column(String(40), nullable=False, unique=True, index=True)

    user_id: Mapped[int] = mapped_column(
        ForeignKey("users.id", ondelete="CASCADE"), nullable=False, index=True
    )
    product_id: Mapped[int] = mapped_column(
        ForeignKey("products.id", ondelete="RESTRICT"), nullable=False, index=True
    )

    # Integer toman. `amount` is the product price at order time; `final_amount`
    # is what the user must pay (amount - discount_amount).
    amount: Mapped[int] = mapped_column(BigInteger, nullable=False, default=0)
    discount_amount: Mapped[int] = mapped_column(BigInteger, nullable=False, default=0)
    final_amount: Mapped[int] = mapped_column(BigInteger, nullable=False, default=0)

    status: Mapped[str] = mapped_column(
        String(32), nullable=False, default="pending_payment", index=True
    )
    payment_method: Mapped[str] = mapped_column(
        String(16), nullable=False, default="card_to_card"
    )

    # Set when an admin acts on the order (later phase).
    admin_id: Mapped[int | None] = mapped_column(
        ForeignKey("admins.id", ondelete="SET NULL"), nullable=True
    )
    reject_reason: Mapped[str | None] = mapped_column(Text, nullable=True)
    user_note: Mapped[str | None] = mapped_column(Text, nullable=True)

    # Service-action orders (Phase 8): a renew/add-traffic order carries the
    # action and the existing V2RayService it targets. `action_type` is null for
    # ordinary new-service / license orders.
    action_type: Mapped[str | None] = mapped_column(String(20), nullable=True)
    target_service_id: Mapped[int | None] = mapped_column(
        ForeignKey("v2ray_services.id", ondelete="SET NULL"), nullable=True, index=True
    )

    # A non-secret summary of what was delivered (e.g. "license #12 · a@b.com").
    # Never contains a password. Filled by delivery_service.
    delivered_payload: Mapped[str | None] = mapped_column(Text, nullable=True)
    # Set when an approved order could not be delivered (empty stock, send failed)
    # so an admin can retry. Cleared on a successful (re)delivery.
    delivery_error: Mapped[str | None] = mapped_column(Text, nullable=True)

    # Lifecycle timestamps (created_at/updated_at come from TimestampMixin).
    paid_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    approved_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    rejected_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    cancelled_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    delivered_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    # Refund foundation (Phase 7).
    refunded_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    refund_reason: Mapped[str | None] = mapped_column(Text, nullable=True)

    # Eager (selectin) so templates/bot can read related rows after the session
    # is closed without triggering a lazy load in async context.
    user: Mapped["User"] = relationship("User", lazy="selectin")
    product: Mapped["Product"] = relationship("Product", lazy="selectin")
    payment: Mapped["Payment | None"] = relationship(
        "Payment", back_populates="order", uselist=False, lazy="selectin",
        cascade="all, delete-orphan",
    )

    def __repr__(self) -> str:  # pragma: no cover - debug helper
        return f"<Order id={self.id} number={self.order_number!r} status={self.status}>"

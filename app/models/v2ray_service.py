"""V2Ray services: one provisioned 3X-UI client per delivered v2ray order.

Created by app/services/v2ray_service.py after a client is written to the panel
AND verified. `order_id` is unique so a single order can never own two clients;
`client_email` is deterministic (dc-u{user}-o{order}) so a retry repairs the
local row instead of creating a duplicate on the panel.

Nothing secret lives here — no UUIDs are treated as credentials-at-rest beyond
the panel's own storage, and no XUI password/token is ever copied in.
"""
from __future__ import annotations

from datetime import datetime
from typing import TYPE_CHECKING

from sqlalchemy import (
    BigInteger,
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
    from app.models.order import Order
    from app.models.product import Product
    from app.models.user import User
    from app.models.xui_inbound import XuiInbound
    from app.models.xui_server import XuiServer

# Service lifecycle.
V2RAY_SERVICE_STATUSES: tuple[str, ...] = (
    "provisioning",  # row created, panel write in progress / being retried
    "active",        # client verified on the panel, delivered
    "disabled",      # admin-disabled on the panel
    "expired",       # past expire_at
    "over_quota",    # used_gb >= total_gb (Phase 8)
    "deleted",       # removed from the panel
    "failed",        # provisioning failed; awaits retry
)


class V2RayService(Base, TimestampMixin):
    __tablename__ = "v2ray_services"
    __table_args__ = (
        # A single order owns at most one client.
        UniqueConstraint("order_id", name="uq_v2ray_services_order"),
        # A client email is unique within a given server+inbound.
        UniqueConstraint(
            "xui_server_id", "xui_inbound_id", "client_email",
            name="uq_v2ray_services_server_inbound_email",
        ),
    )

    id: Mapped[int] = mapped_column(primary_key=True)

    user_id: Mapped[int] = mapped_column(
        ForeignKey("users.id", ondelete="CASCADE"), nullable=False, index=True
    )
    order_id: Mapped[int] = mapped_column(
        ForeignKey("orders.id", ondelete="CASCADE"), nullable=False, index=True
    )
    product_id: Mapped[int] = mapped_column(
        ForeignKey("products.id", ondelete="RESTRICT"), nullable=False, index=True
    )
    xui_server_id: Mapped[int] = mapped_column(
        ForeignKey("xui_servers.id", ondelete="RESTRICT"), nullable=False, index=True
    )
    xui_inbound_id: Mapped[int] = mapped_column(
        ForeignKey("xui_inbounds.id", ondelete="RESTRICT"), nullable=False, index=True
    )

    client_email: Mapped[str] = mapped_column(String(255), nullable=False, index=True)
    client_uuid: Mapped[str] = mapped_column(String(64), nullable=False)
    sub_id: Mapped[str | None] = mapped_column(String(64), nullable=True)
    subscription_url: Mapped[str | None] = mapped_column(Text, nullable=True)
    qr_code_path: Mapped[str | None] = mapped_column(Text, nullable=True)

    # Bytes; 0 = unlimited (panel convention). used_gb is a synced counter.
    total_gb: Mapped[int] = mapped_column(BigInteger, nullable=False, default=0)
    used_gb: Mapped[int] = mapped_column(BigInteger, nullable=False, default=0)
    ip_limit: Mapped[int | None] = mapped_column(Integer, nullable=True)

    expire_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)

    status: Mapped[str] = mapped_column(
        String(16), nullable=False, default="provisioning", index=True
    )
    last_error: Mapped[str | None] = mapped_column(Text, nullable=True)

    last_traffic_sync_at: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True), nullable=True
    )
    provisioned_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    disabled_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    deleted_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)

    # Lifecycle (Phase 8): when the service crossed expiry / quota, and when the
    # user was last warned (so a warning fires at most once per period).
    expired_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    over_quota_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    last_expiry_warning_at: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True), nullable=True
    )
    last_traffic_warning_at: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True), nullable=True
    )

    # Eager (selectin) so templates/bot read related rows after the session closes.
    user: Mapped["User"] = relationship("User", lazy="selectin")
    # Disambiguate: `orders.target_service_id` (Phase 8) also links the two tables,
    # so pin this relationship to the owning-order FK.
    order: Mapped["Order"] = relationship(
        "Order", lazy="selectin", foreign_keys=[order_id]
    )
    product: Mapped["Product"] = relationship("Product", lazy="selectin")
    xui_server: Mapped["XuiServer"] = relationship("XuiServer", lazy="selectin")
    xui_inbound: Mapped["XuiInbound"] = relationship("XuiInbound", lazy="selectin")

    def __repr__(self) -> str:  # pragma: no cover - debug helper
        return (f"<V2RayService id={self.id} order={self.order_id} "
                f"email={self.client_email!r} status={self.status}>")

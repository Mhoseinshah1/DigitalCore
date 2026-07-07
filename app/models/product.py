"""Products: sellable items of type "license" or "v2ray".

Price is stored as an INTEGER amount of toman (no minor units) — the platform's
consistent money convention. server_id / inbound_id are plain stored ints for
now; they are validated against a real 3X-UI panel in a later phase.
"""
from __future__ import annotations

from sqlalchemy import BigInteger, Boolean, ForeignKey, Integer, String, Text
from sqlalchemy.orm import Mapped, mapped_column

from app.models.base import Base, TimestampMixin

PRODUCT_TYPES: tuple[str, ...] = ("license", "v2ray")

# What a v2ray product does when bought (Phase 8). `new_service` (default/None)
# provisions a fresh client; `renew_service` / `add_traffic` modify an existing
# V2RayService the buyer already owns.
PRODUCT_ACTION_TYPES: tuple[str, ...] = ("new_service", "renew_service", "add_traffic")


class Product(Base, TimestampMixin):
    __tablename__ = "products"

    id: Mapped[int] = mapped_column(primary_key=True)

    type: Mapped[str] = mapped_column(String(16), nullable=False, index=True)
    title: Mapped[str] = mapped_column(String(200), nullable=False)
    description: Mapped[str | None] = mapped_column(Text, nullable=True)

    # Integer toman.
    price: Mapped[int] = mapped_column(BigInteger, nullable=False, default=0)

    # v2ray-specific (required > 0 for v2ray; ignored for license).
    duration_days: Mapped[int | None] = mapped_column(Integer, nullable=True)
    traffic_gb: Mapped[int | None] = mapped_column(Integer, nullable=True)
    ip_limit: Mapped[int | None] = mapped_column(Integer, nullable=True)

    # Service-action support (Phase 8). action_type is one of PRODUCT_ACTION_TYPES;
    # applies_to_service marks renew/add-traffic products that modify an existing
    # V2RayService instead of creating a new client.
    action_type: Mapped[str | None] = mapped_column(String(20), nullable=True)
    applies_to_service: Mapped[bool] = mapped_column(
        Boolean, nullable=False, default=False, server_default="0"
    )

    # Legacy plain-int references (panel-side ids); superseded by the FK bindings
    # below and kept only for backward compatibility.
    server_id: Mapped[int | None] = mapped_column(Integer, nullable=True)
    inbound_id: Mapped[int | None] = mapped_column(Integer, nullable=True)

    # V2Ray binding: which stored XuiServer record + XuiInbound record this
    # product provisions on. Required for type=="v2ray", null for licenses.
    xui_server_id: Mapped[int | None] = mapped_column(
        ForeignKey("xui_servers.id", ondelete="SET NULL"), nullable=True, index=True
    )
    xui_inbound_id: Mapped[int | None] = mapped_column(
        ForeignKey("xui_inbounds.id", ondelete="SET NULL"), nullable=True, index=True
    )

    is_active: Mapped[bool] = mapped_column(Boolean, default=True, nullable=False)
    is_hidden: Mapped[bool] = mapped_column(Boolean, default=False, nullable=False)

    stock_count: Mapped[int] = mapped_column(Integer, default=0, nullable=False)
    sort_order: Mapped[int] = mapped_column(Integer, default=0, nullable=False)

    def __repr__(self) -> str:  # pragma: no cover - debug helper
        return f"<Product id={self.id} type={self.type} title={self.title!r}>"

"""Inbounds for a 3X-UI server (one row per panel inbound; synced or manual)."""
from __future__ import annotations

from typing import TYPE_CHECKING

from datetime import datetime

from sqlalchemy import Boolean, DateTime, ForeignKey, Integer, String, Text, UniqueConstraint
from sqlalchemy.orm import Mapped, mapped_column, relationship

from app.models.base import Base, TimestampMixin

if TYPE_CHECKING:
    from app.models.xui_server import XuiServer


class XuiInbound(Base, TimestampMixin):
    __tablename__ = "xui_inbounds"
    __table_args__ = (
        UniqueConstraint("server_id", "inbound_id", name="uq_xui_inbounds_server_inbound"),
    )

    id: Mapped[int] = mapped_column(primary_key=True)
    server_id: Mapped[int] = mapped_column(
        ForeignKey("xui_servers.id", ondelete="CASCADE"), nullable=False, index=True
    )
    inbound_id: Mapped[int] = mapped_column(Integer, nullable=False)  # id on the panel
    remark: Mapped[str | None] = mapped_column(String(255), nullable=True)
    protocol: Mapped[str | None] = mapped_column(String(32), nullable=True)
    port: Mapped[int | None] = mapped_column(Integer, nullable=True)
    network: Mapped[str | None] = mapped_column(String(32), nullable=True)
    security: Mapped[str | None] = mapped_column(String(32), nullable=True)
    tag: Mapped[str | None] = mapped_column(String(255), nullable=True)
    is_active: Mapped[bool] = mapped_column(Boolean, default=True, nullable=False)
    # Whether the inbound is enabled on the panel (mirror of the panel's `enable`).
    enable_from_panel: Mapped[bool] = mapped_column(
        Boolean, default=True, server_default="1", nullable=False
    )
    # Full inbound JSON from the last sync (diagnostic) + when it was synced.
    raw_json: Mapped[str | None] = mapped_column(Text, nullable=True)
    synced_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)

    server: Mapped["XuiServer"] = relationship("XuiServer", back_populates="inbounds")

    def __repr__(self) -> str:  # pragma: no cover - debug helper
        return f"<XuiInbound server={self.server_id} inbound={self.inbound_id} remark={self.remark!r}>"

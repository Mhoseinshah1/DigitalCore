"""3X-UI panel server records.

Credentials (panel password, optional API token) are stored as Fernet
ciphertext via app/core/crypto.py and are never logged or rendered in plaintext.
"""
from __future__ import annotations

from datetime import datetime
from typing import TYPE_CHECKING

from sqlalchemy import Boolean, DateTime, String, Text
from sqlalchemy.orm import Mapped, mapped_column, relationship

from app.models.base import Base, TimestampMixin

if TYPE_CHECKING:
    from app.models.xui_inbound import XuiInbound

# `status` reflects the last health check. The first four are the Phase 2.1
# vocabulary; the low-level health-check service (app/services/xui_service.py)
# also uses online/offline/auth_error, so both sets are accepted here.
XUI_STATUSES: tuple[str, ...] = (
    "unknown", "active", "inactive", "error",
    "online", "offline", "auth_error",
)


class XuiServer(Base, TimestampMixin):
    __tablename__ = "xui_servers"

    id: Mapped[int] = mapped_column(primary_key=True)
    name: Mapped[str] = mapped_column(String(120), nullable=False)
    base_url: Mapped[str] = mapped_column(String(255), nullable=False)
    web_base_path: Mapped[str | None] = mapped_column(String(120), nullable=True)

    panel_type: Mapped[str] = mapped_column(String(32), default="3x-ui", nullable=False)
    panel_version: Mapped[str] = mapped_column(String(32), default="2.9.4", nullable=False)

    username: Mapped[str | None] = mapped_column(String(120), nullable=True)
    encrypted_password: Mapped[str | None] = mapped_column(Text, nullable=True)
    encrypted_api_token: Mapped[str | None] = mapped_column(Text, nullable=True)

    status: Mapped[str] = mapped_column(String(16), default="unknown", nullable=False)
    # Admin on/off switch, independent of the health `status`.
    is_active: Mapped[bool] = mapped_column(
        Boolean, default=True, server_default="1", nullable=False
    )

    # Optional public subscription host + path (Phase 6). The panel's
    # subscription service usually listens on a different host/port/path than
    # the admin API, so we never guess it from base_url. Null until an admin
    # sets it; without it a service's subscription_url is left null.
    public_sub_base_url: Mapped[str | None] = mapped_column(String(255), nullable=True)
    subscription_path: Mapped[str | None] = mapped_column(String(120), nullable=True)
    last_health_check: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True), nullable=True
    )
    last_error: Mapped[str | None] = mapped_column(Text, nullable=True)

    inbounds: Mapped[list["XuiInbound"]] = relationship(
        "XuiInbound", back_populates="server", cascade="all, delete-orphan"
    )

    def __repr__(self) -> str:  # pragma: no cover - debug helper
        return f"<XuiServer id={self.id} name={self.name!r} version={self.panel_version}>"

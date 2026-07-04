"""3X-UI panel server records.

Credentials (panel password, optional API token) are stored as Fernet
ciphertext via app/core/crypto.py and are never logged or rendered in plaintext.
"""
from __future__ import annotations

from datetime import datetime

from sqlalchemy import DateTime, String, Text
from sqlalchemy.orm import Mapped, mapped_column

from app.models.base import Base, TimestampMixin

XUI_STATUSES: tuple[str, ...] = ("unknown", "online", "offline", "auth_error")


class XuiServer(Base, TimestampMixin):
    __tablename__ = "xui_servers"

    id: Mapped[int] = mapped_column(primary_key=True)
    name: Mapped[str] = mapped_column(String(120), nullable=False)
    base_url: Mapped[str] = mapped_column(String(255), nullable=False)
    web_base_path: Mapped[str | None] = mapped_column(String(120), nullable=True)

    panel_type: Mapped[str] = mapped_column(String(32), default="3x-ui", nullable=False)
    panel_version: Mapped[str] = mapped_column(String(32), default="2.9.4", nullable=False)

    username: Mapped[str] = mapped_column(String(120), nullable=False)
    encrypted_password: Mapped[str] = mapped_column(Text, nullable=False)
    encrypted_api_token: Mapped[str | None] = mapped_column(Text, nullable=True)

    status: Mapped[str] = mapped_column(String(16), default="unknown", nullable=False)
    last_health_check: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True), nullable=True
    )

    def __repr__(self) -> str:  # pragma: no cover - debug helper
        return f"<XuiServer id={self.id} name={self.name!r} version={self.panel_version}>"

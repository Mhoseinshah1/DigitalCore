"""Key/value business settings.

Every business setting the platform understands is stored as a row here. Rows are
seeded on first boot with empty/default values and are edited from the admin
panel. Secret-flagged values are stored encrypted at rest with the FERNET_KEY.
"""
from __future__ import annotations

from sqlalchemy import Boolean, String, Text
from sqlalchemy.orm import Mapped, mapped_column

from app.models.base import Base, TimestampMixin


class Setting(Base, TimestampMixin):
    __tablename__ = "settings"

    key: Mapped[str] = mapped_column(String(64), primary_key=True)

    # Raw stored value. For secret settings this is the Fernet ciphertext.
    value: Mapped[str] = mapped_column(Text, default="", nullable=False)

    # Grouping used to lay out the panel Settings page.
    category: Mapped[str] = mapped_column(String(32), default="general", nullable=False)

    # Value type hint for the panel UI: string | text | bool | int | secret.
    value_type: Mapped[str] = mapped_column(String(16), default="string", nullable=False)

    # When true, `value` is encrypted at rest and masked in the API.
    is_secret: Mapped[bool] = mapped_column(Boolean, default=False, nullable=False)

    label: Mapped[str] = mapped_column(String(128), default="", nullable=False)
    description: Mapped[str] = mapped_column(String(255), default="", nullable=False)

    def __repr__(self) -> str:  # pragma: no cover - debug helper
        return f"<Setting key={self.key} category={self.category}>"

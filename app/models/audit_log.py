"""Audit log: who did what, to what, with the before/after values."""
from __future__ import annotations

from datetime import datetime

from sqlalchemy import BigInteger, DateTime, String, Text, func
from sqlalchemy.orm import Mapped, mapped_column

from app.models.base import Base


class AuditLog(Base):
    __tablename__ = "audit_logs"

    id: Mapped[int] = mapped_column(primary_key=True)

    # user | admin | system
    actor_type: Mapped[str] = mapped_column(String(16), nullable=False)
    # Telegram id for users, admins.id for admins, NULL for system.
    actor_id: Mapped[int | None] = mapped_column(BigInteger, nullable=True)

    action: Mapped[str] = mapped_column(String(128), nullable=False, index=True)

    target_type: Mapped[str | None] = mapped_column(String(64), nullable=True)
    target_id: Mapped[str | None] = mapped_column(String(64), nullable=True)

    old_value: Mapped[str | None] = mapped_column(Text, nullable=True)
    new_value: Mapped[str | None] = mapped_column(Text, nullable=True)

    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), nullable=False, index=True
    )

    def __repr__(self) -> str:  # pragma: no cover - debug helper
        return f"<AuditLog id={self.id} actor={self.actor_type}:{self.actor_id} action={self.action}>"

"""One message in a support ticket thread (Phase 9).

`sender_type` is user / admin / system; exactly one of `sender_user_id` /
`sender_admin_id` is set for the first two (system messages set neither). An
optional attachment is stored on disk under ``storage/tickets/YYYY/MM/`` — only
its metadata + a tickets-root-relative path live here, never the bytes.
"""
from __future__ import annotations

from typing import TYPE_CHECKING

from sqlalchemy import ForeignKey, Integer, String, Text
from sqlalchemy.orm import Mapped, mapped_column, relationship

from app.models.base import Base, TimestampMixin

if TYPE_CHECKING:
    from app.models.ticket import Ticket

SENDER_TYPES: tuple[str, ...] = ("user", "admin", "system")


class TicketMessage(Base, TimestampMixin):
    __tablename__ = "ticket_messages"

    id: Mapped[int] = mapped_column(primary_key=True)
    ticket_id: Mapped[int] = mapped_column(
        ForeignKey("tickets.id", ondelete="CASCADE"), nullable=False, index=True
    )

    sender_type: Mapped[str] = mapped_column(String(10), nullable=False)
    sender_user_id: Mapped[int | None] = mapped_column(
        ForeignKey("users.id", ondelete="SET NULL"), nullable=True
    )
    sender_admin_id: Mapped[int | None] = mapped_column(
        ForeignKey("admins.id", ondelete="SET NULL"), nullable=True
    )

    message: Mapped[str] = mapped_column(Text, nullable=False, default="")

    # Attachment (same shape as a Payment receipt): Telegram file_id, a
    # tickets-root-relative on-disk path, and metadata to serve it safely.
    attachment_path: Mapped[str | None] = mapped_column(String(512), nullable=True)
    attachment_file_id: Mapped[str | None] = mapped_column(String(255), nullable=True)
    attachment_mime_type: Mapped[str | None] = mapped_column(String(128), nullable=True)
    attachment_original_name: Mapped[str | None] = mapped_column(String(255), nullable=True)
    attachment_size: Mapped[int | None] = mapped_column(Integer, nullable=True)

    ticket: Mapped["Ticket"] = relationship("Ticket", back_populates="messages")

    @property
    def has_attachment(self) -> bool:
        return bool(self.attachment_path)

    def __repr__(self) -> str:  # pragma: no cover - debug helper
        return (f"<TicketMessage id={self.id} ticket_id={self.ticket_id} "
                f"sender={self.sender_type}>")

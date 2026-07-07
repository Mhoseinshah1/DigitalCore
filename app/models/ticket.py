"""Support tickets (Phase 9): a user's conversation thread with staff.

A ticket owns an ordered list of `TicketMessage` rows (user / admin / system).
`ticket_number` is a readable unique id (``TK-YYYYMMDD-000001``). Status tracks
who the ball is with: a user reply flips it to `pending_admin`, an admin reply to
`pending_user`; either party can `close` it and (if the setting allows) the user
can `reopen`. `last_message_at` powers the "recently active" ordering.

Attachment BYTES never live here — only metadata + a tickets-root-relative path
on each message (see ticket_service); the serving layer re-validates containment.
"""
from __future__ import annotations

from datetime import datetime
from typing import TYPE_CHECKING

from sqlalchemy import DateTime, ForeignKey, String, Text
from sqlalchemy.orm import Mapped, mapped_column, relationship

from app.models.base import Base, TimestampMixin

if TYPE_CHECKING:
    from app.models.admin import Admin
    from app.models.ticket_message import TicketMessage
    from app.models.user import User

TICKET_STATUSES: tuple[str, ...] = (
    "open",            # newly created, not yet actioned
    "pending_admin",   # awaiting a staff reply (user spoke last)
    "pending_user",    # awaiting the user (staff spoke last)
    "closed",          # resolved
)

TICKET_PRIORITIES: tuple[str, ...] = ("low", "normal", "high", "urgent")


class Ticket(Base, TimestampMixin):
    __tablename__ = "tickets"

    id: Mapped[int] = mapped_column(primary_key=True)
    ticket_number: Mapped[str] = mapped_column(
        String(40), nullable=False, unique=True, index=True
    )
    user_id: Mapped[int] = mapped_column(
        ForeignKey("users.id", ondelete="CASCADE"), nullable=False, index=True
    )
    subject: Mapped[str] = mapped_column(String(200), nullable=False)

    status: Mapped[str] = mapped_column(
        String(20), nullable=False, default="open", index=True
    )
    priority: Mapped[str] = mapped_column(
        String(10), nullable=False, default="normal", index=True
    )
    assigned_admin_id: Mapped[int | None] = mapped_column(
        ForeignKey("admins.id", ondelete="SET NULL"), nullable=True, index=True
    )

    closed_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    last_message_at: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True), nullable=True
    )

    # Eager (selectin) so templates/bot read related rows after the session closes.
    user: Mapped["User"] = relationship("User", lazy="selectin")
    assigned_admin: Mapped["Admin | None"] = relationship("Admin", lazy="selectin")
    messages: Mapped[list["TicketMessage"]] = relationship(
        "TicketMessage",
        back_populates="ticket",
        order_by="TicketMessage.id",
        cascade="all, delete-orphan",
        lazy="selectin",
    )

    def __repr__(self) -> str:  # pragma: no cover - debug helper
        return (f"<Ticket id={self.id} number={self.ticket_number!r} "
                f"status={self.status} priority={self.priority}>")

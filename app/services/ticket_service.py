"""Support tickets (Phase 9): create, reply, close/reopen, assign, prioritise.

A ticket is a thread of `TicketMessage` rows. Ownership is strict — a user only
ever touches their own tickets (`add_user_reply` / `reopen_ticket` verify it);
staff act through the admin functions gated by the web/bot RBAC layer.

Attachments reuse the Payment-receipt discipline: bytes go to disk under
``storage/tickets/YYYY/MM/`` (never the DB or the audit log), only metadata + a
tickets-root-relative path are stored, filenames are sanitised, and the serving
layer re-validates containment against path traversal. Type + size are validated
(images / pdf / common docs, default max 10 MB, overridable via settings).
"""
from __future__ import annotations

import logging
import re
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path

from sqlalchemy import func, select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.settings_service import SettingsService
from app.models.ticket import TICKET_PRIORITIES, TICKET_STATUSES, Ticket
from app.models.ticket_message import TicketMessage
from app.services import audit_service
from app.services.payment_service import _ext_of, _sanitize_filename

log = logging.getLogger("ticket")

# storage/tickets/ at the repo root. Overridable in tests via monkeypatch.
TICKETS_ROOT: Path = Path(__file__).resolve().parents[2] / "storage" / "tickets"

DEFAULT_MAX_ATTACHMENT_MB = 10
# Safe, non-executable attachment types.
ALLOWED_EXTENSIONS: frozenset[str] = frozenset({
    "jpg", "jpeg", "png", "webp", "gif", "pdf", "txt", "log", "zip", "doc", "docx",
})
_MIME_FOR_EXT = {
    "jpg": "image/jpeg", "jpeg": "image/jpeg", "png": "image/png",
    "webp": "image/webp", "gif": "image/gif", "pdf": "application/pdf",
    "txt": "text/plain", "log": "text/plain", "zip": "application/zip",
    "doc": "application/msword",
    "docx": "application/vnd.openxmlformats-officedocument.wordprocessingml.document",
}


class TicketError(ValueError):
    """A user-facing reason a ticket action was refused."""

    code = "ticket_error"

    def __init__(self, message: str, code: str | None = None) -> None:
        super().__init__(message)
        if code is not None:
            self.code = code


@dataclass
class TicketAttachment:
    """A downloaded Telegram file handed to the service for storage."""

    content: bytes
    original_name: str
    mime_type: str | None = None
    file_id: str | None = None

    @property
    def size(self) -> int:
        return len(self.content)


def _now() -> datetime:
    return datetime.now(timezone.utc)


# --------------------------------------------------------------------------
# Settings
# --------------------------------------------------------------------------
async def support_enabled(session: AsyncSession) -> bool:
    return await SettingsService(session).get_bool("support_enabled", True)


async def attachments_enabled(session: AsyncSession) -> bool:
    return await SettingsService(session).get_bool("ticket_attachments_enabled", True)


async def _max_attachment_bytes(session: AsyncSession) -> int:
    mb = await SettingsService(session).get_int("max_ticket_attachment_mb",
                                                DEFAULT_MAX_ATTACHMENT_MB)
    return max(1, mb) * 1024 * 1024


async def _reopen_allowed(session: AsyncSession) -> bool:
    return await SettingsService(session).get_bool("allow_reopen_closed_tickets", True)


# --------------------------------------------------------------------------
# Attachment validation + storage
# --------------------------------------------------------------------------
def precheck_attachment(original_name: str, size: int | None, mime_type: str | None = None) -> None:
    """Cheap pre-download guard: reject bad types and known-oversize files."""
    ext = _ext_of(original_name, mime_type)
    if ext not in ALLOWED_EXTENSIONS:
        raise TicketError("unsupported file type", code="unsupported_type")
    if size and size > DEFAULT_MAX_ATTACHMENT_MB * 1024 * 1024 * 4:
        # Hard ceiling well above any configured limit; the exact check runs
        # against the setting once the bytes are in hand.
        raise TicketError("the file is too large", code="too_large")


async def validate_attachment(
    session: AsyncSession, original_name: str, size: int, mime_type: str | None = None
) -> str:
    """Validate type + size against settings; return the accepted extension."""
    if size <= 0:
        raise TicketError("the file is empty", code="empty")
    if size > await _max_attachment_bytes(session):
        raise TicketError("the file is too large", code="too_large")
    ext = _ext_of(original_name, mime_type)
    if ext not in ALLOWED_EXTENSIONS:
        raise TicketError("unsupported file type", code="unsupported_type")
    return ext


def _attachment_relpath(ticket_number: str, original_name: str, mime_type: str | None,
                        when: datetime) -> str:
    """Relative path under TICKETS_ROOT: ``YYYY/MM/<ticket>_<epochish>_<safe>``."""
    ext = _ext_of(original_name, mime_type) or "bin"
    safe_name = _sanitize_filename(original_name, fallback_ext=ext)
    safe_ticket = re.sub(r"[^A-Za-z0-9._-]", "_", ticket_number or "ticket")
    # microsecond suffix keeps two same-name uploads on one ticket distinct.
    stamp = f"{when.hour:02d}{when.minute:02d}{when.second:02d}{when.microsecond:06d}"
    return f"{when.year:04d}/{when.month:02d}/{safe_ticket}_{stamp}_{safe_name}"


def resolve_attachment_path(stored_rel: str | None) -> Path | None:
    """Resolve a stored relative path to an absolute file, guarding traversal."""
    if not stored_rel:
        return None
    root = TICKETS_ROOT.resolve()
    candidate = (root / stored_rel).resolve()
    try:
        candidate.relative_to(root)
    except ValueError:
        return None  # path traversal attempt
    if not candidate.is_file():
        return None
    return candidate


async def _store_attachment(
    session: AsyncSession, ticket: Ticket, attachment: TicketAttachment, when: datetime
) -> dict:
    """Validate + write the attachment; return the message's attachment_* fields."""
    if not await attachments_enabled(session):
        raise TicketError("attachments are disabled", code="attachments_disabled")
    ext = await validate_attachment(
        session, attachment.original_name, attachment.size, attachment.mime_type)
    rel = _attachment_relpath(ticket.ticket_number, attachment.original_name,
                              attachment.mime_type, when)
    dest = TICKETS_ROOT / rel
    dest.parent.mkdir(parents=True, exist_ok=True)
    dest.write_bytes(attachment.content)
    return {
        "attachment_path": rel,
        "attachment_file_id": attachment.file_id,
        "attachment_mime_type": attachment.mime_type or _MIME_FOR_EXT.get(ext),
        "attachment_original_name": attachment.original_name,
        "attachment_size": attachment.size,
    }


# --------------------------------------------------------------------------
# Numbering + queries
# --------------------------------------------------------------------------
async def generate_ticket_number(session: AsyncSession) -> str:
    today = _now().strftime("%Y%m%d")
    prefix = f"TK-{today}-"
    count = await session.scalar(
        select(func.count(Ticket.id)).where(Ticket.ticket_number.like(f"{prefix}%"))
    )
    return f"{prefix}{(count or 0) + 1:06d}"


async def get_ticket(session: AsyncSession, ticket_id: int) -> Ticket | None:
    return await session.get(Ticket, ticket_id)


async def get_ticket_by_number(session: AsyncSession, ticket_number: str) -> Ticket | None:
    return await session.scalar(
        select(Ticket).where(Ticket.ticket_number == ticket_number)
    )


async def list_user_tickets(
    session: AsyncSession, user_id: int, *, limit: int = 50
) -> list[Ticket]:
    stmt = (select(Ticket).where(Ticket.user_id == user_id)
            .order_by(Ticket.last_message_at.desc().nulls_last(), Ticket.id.desc())
            .limit(limit))
    return list((await session.execute(stmt)).scalars().all())


async def list_admin_tickets(
    session: AsyncSession, *, status: str | None = None,
    assigned_admin_id: int | None = None, limit: int = 100, offset: int = 0,
) -> list[Ticket]:
    stmt = select(Ticket)
    if status == "open":
        # "Open" in the admin queue means anything not yet closed.
        stmt = stmt.where(Ticket.status != "closed")
    elif status:
        stmt = stmt.where(Ticket.status == status)
    if assigned_admin_id is not None:
        stmt = stmt.where(Ticket.assigned_admin_id == assigned_admin_id)
    stmt = (stmt.order_by(Ticket.last_message_at.desc().nulls_last(), Ticket.id.desc())
            .limit(limit).offset(offset))
    return list((await session.execute(stmt)).scalars().all())


async def count_by_status(session: AsyncSession) -> dict[str, int]:
    rows = await session.execute(
        select(Ticket.status, func.count(Ticket.id)).group_by(Ticket.status)
    )
    return {status: count for status, count in rows.all()}


# --------------------------------------------------------------------------
# Message helper
# --------------------------------------------------------------------------
async def _add_message(
    session: AsyncSession, ticket: Ticket, *, sender_type: str, message: str,
    sender_user_id: int | None = None, sender_admin_id: int | None = None,
    attachment: TicketAttachment | None = None, when: datetime,
) -> TicketMessage:
    fields: dict = {}
    if attachment is not None:
        fields = await _store_attachment(session, ticket, attachment, when)
    msg = TicketMessage(
        ticket_id=ticket.id, sender_type=sender_type, message=(message or "").strip(),
        sender_user_id=sender_user_id, sender_admin_id=sender_admin_id, **fields,
    )
    session.add(msg)
    ticket.last_message_at = when
    return msg


# --------------------------------------------------------------------------
# Create + replies
# --------------------------------------------------------------------------
async def create_ticket(
    session: AsyncSession, user_id: int, subject: str, message: str,
    *, attachment: TicketAttachment | None = None, priority: str = "normal",
) -> Ticket:
    """Open a new ticket with its first (user) message."""
    if not await support_enabled(session):
        raise TicketError("support is disabled", code="support_disabled")
    subject = (subject or "").strip()
    if not subject:
        raise TicketError("subject is required", code="subject_required")
    if len(subject) > 200:
        subject = subject[:200]
    if not (message or "").strip() and attachment is None:
        raise TicketError("a message is required", code="message_required")
    if priority not in TICKET_PRIORITIES:
        priority = "normal"

    now = _now()
    ticket: Ticket | None = None
    for _ in range(5):  # retry only on the rare ticket-number collision
        number = await generate_ticket_number(session)
        candidate = Ticket(
            ticket_number=number, user_id=user_id, subject=subject,
            status="pending_admin", priority=priority, last_message_at=now,
        )
        session.add(candidate)
        try:
            await session.flush()
        except IntegrityError:
            await session.rollback()
            continue
        ticket = candidate
        break
    if ticket is None:
        raise TicketError("could not allocate a ticket number", code="ticket_error")

    await _add_message(session, ticket, sender_type="user", message=message,
                       sender_user_id=user_id, attachment=attachment, when=now)
    await audit_service.log(
        session, actor_type="user", actor_id=user_id, action="ticket_created",
        target_type="ticket", target_id=ticket.id,
        new=f"number={ticket.ticket_number} subject={subject[:60]!r}",
    )
    await session.refresh(ticket)
    return ticket


async def add_user_reply(
    session: AsyncSession, ticket_id: int, user_id: int, message: str,
    *, attachment: TicketAttachment | None = None,
) -> Ticket:
    """Append a user reply. Verifies ownership; flips status to pending_admin."""
    ticket = await get_ticket(session, ticket_id)
    if ticket is None:
        raise TicketError("ticket not found", code="ticket_not_found")
    if ticket.user_id != user_id:
        raise TicketError("not your ticket", code="not_your_ticket")
    if ticket.status == "closed":
        raise TicketError("ticket is closed", code="ticket_closed")
    if not (message or "").strip() and attachment is None:
        raise TicketError("a message is required", code="message_required")

    now = _now()
    await _add_message(session, ticket, sender_type="user", message=message,
                       sender_user_id=user_id, attachment=attachment, when=now)
    ticket.status = "pending_admin"
    await audit_service.log(
        session, actor_type="user", actor_id=user_id, action="ticket_user_replied",
        target_type="ticket", target_id=ticket.id, meta=f"number={ticket.ticket_number}",
    )
    await session.refresh(ticket)
    return ticket


async def add_admin_reply(
    session: AsyncSession, ticket_id: int, admin_id: int, message: str,
    *, attachment: TicketAttachment | None = None,
) -> Ticket:
    """Append a staff reply. Flips status to pending_user (awaiting the user)."""
    ticket = await get_ticket(session, ticket_id)
    if ticket is None:
        raise TicketError("ticket not found", code="ticket_not_found")
    if not (message or "").strip() and attachment is None:
        raise TicketError("a message is required", code="message_required")

    now = _now()
    await _add_message(session, ticket, sender_type="admin", message=message,
                       sender_admin_id=admin_id, attachment=attachment, when=now)
    if ticket.status != "closed":
        ticket.status = "pending_user"
    await audit_service.log(
        session, actor_type="admin", actor_id=admin_id, action="ticket_admin_replied",
        target_type="ticket", target_id=ticket.id, meta=f"number={ticket.ticket_number}",
    )
    await session.refresh(ticket)
    return ticket


# --------------------------------------------------------------------------
# Lifecycle
# --------------------------------------------------------------------------
async def close_ticket(
    session: AsyncSession, ticket_id: int, *, actor_id: int | None, actor_type: str,
    user_id: int | None = None,
) -> Ticket:
    """Close a ticket. When `user_id` is given (user action) it must own it."""
    ticket = await get_ticket(session, ticket_id)
    if ticket is None:
        raise TicketError("ticket not found", code="ticket_not_found")
    if user_id is not None and ticket.user_id != user_id:
        raise TicketError("not your ticket", code="not_your_ticket")
    if ticket.status == "closed":
        return ticket
    ticket.status = "closed"
    ticket.closed_at = _now()
    await audit_service.log(
        session, actor_type=actor_type, actor_id=actor_id, action="ticket_closed",
        target_type="ticket", target_id=ticket.id, meta=f"number={ticket.ticket_number}",
    )
    await session.refresh(ticket)
    return ticket


async def reopen_ticket(session: AsyncSession, ticket_id: int, user_id: int) -> Ticket:
    """Reopen a closed ticket (user action) if the setting allows it."""
    ticket = await get_ticket(session, ticket_id)
    if ticket is None:
        raise TicketError("ticket not found", code="ticket_not_found")
    if ticket.user_id != user_id:
        raise TicketError("not your ticket", code="not_your_ticket")
    if ticket.status != "closed":
        return ticket
    if not await _reopen_allowed(session):
        raise TicketError("reopening is disabled", code="reopen_disabled")
    ticket.status = "pending_admin"
    ticket.closed_at = None
    await audit_service.log(
        session, actor_type="user", actor_id=user_id, action="ticket_reopened",
        target_type="ticket", target_id=ticket.id, meta=f"number={ticket.ticket_number}",
    )
    await session.refresh(ticket)
    return ticket


async def assign_ticket(session: AsyncSession, ticket_id: int, admin_id: int) -> Ticket:
    """Assign the ticket to an admin (e.g. assign-to-self)."""
    ticket = await get_ticket(session, ticket_id)
    if ticket is None:
        raise TicketError("ticket not found", code="ticket_not_found")
    ticket.assigned_admin_id = admin_id
    await audit_service.log(
        session, actor_type="admin", actor_id=admin_id, action="ticket_assigned",
        target_type="ticket", target_id=ticket.id,
        meta=f"number={ticket.ticket_number} admin_id={admin_id}",
    )
    await session.refresh(ticket)
    return ticket


async def set_priority(
    session: AsyncSession, ticket_id: int, priority: str, *, admin_id: int | None
) -> Ticket:
    ticket = await get_ticket(session, ticket_id)
    if ticket is None:
        raise TicketError("ticket not found", code="ticket_not_found")
    if priority not in TICKET_PRIORITIES:
        raise TicketError("invalid priority", code="invalid_priority")
    old = ticket.priority
    ticket.priority = priority
    await audit_service.log(
        session, actor_type="admin", actor_id=admin_id, action="ticket_priority_changed",
        target_type="ticket", target_id=ticket.id, old=old, new=priority,
        meta=f"number={ticket.ticket_number}",
    )
    await session.refresh(ticket)
    return ticket


# --------------------------------------------------------------------------
# Best-effort user notification (used by web + telegram admin reply paths)
# --------------------------------------------------------------------------
async def notify_user(bot, user, key: str, **params) -> None:
    """Send a translated Telegram message to a ticket's owner. Never raises."""
    if user is None or not getattr(user, "telegram_id", None):
        return
    from app.i18n import t
    lang = user.language if getattr(user, "language", None) else "fa"
    text = t(key, lang, **params)
    b, own = bot, None
    if b is None:
        from app.config import settings
        if not settings.TELEGRAM_BOT_TOKEN:
            return
        from aiogram import Bot
        own = b = Bot(settings.TELEGRAM_BOT_TOKEN)
    try:
        await b.send_message(user.telegram_id, text, parse_mode="HTML")
    except Exception as exc:  # noqa: BLE001 - notification is best-effort
        log.info("ticket notify failed: %s", exc)
    finally:
        if own is not None:
            try:
                await own.session.close()
            except Exception:  # noqa: BLE001
                pass

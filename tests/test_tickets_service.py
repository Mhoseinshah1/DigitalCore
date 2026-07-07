"""Phase 9: ticket_service — create/reply/close/reopen/assign/priority, ownership,
attachment validation + on-disk storage, and audit logging."""
from __future__ import annotations

import tempfile
from pathlib import Path

import pytest
import pytest_asyncio
from sqlalchemy import select

import app.services.ticket_service as ticket_service
from app.models import AuditLog, User
from app.services.ticket_service import TicketAttachment, TicketError


@pytest_asyncio.fixture(autouse=True)
def _tmp_tickets_root(monkeypatch):
    root = Path(tempfile.mkdtemp()) / "tickets"
    monkeypatch.setattr(ticket_service, "TICKETS_ROOT", root)
    yield root


async def _user(db_session, tg=100) -> User:
    u = User(telegram_id=tg, first_name="U", language="fa")
    db_session.add(u)
    await db_session.flush()
    return u


async def test_create_ticket_with_attachment(db_session) -> None:
    u = await _user(db_session)
    att = TicketAttachment(content=b"img", original_name="p.png", mime_type="image/png", file_id="f")
    tk = await ticket_service.create_ticket(db_session, u.id, "Help", "it broke", attachment=att)
    await db_session.commit()
    assert tk.ticket_number.startswith("TK-")
    assert tk.status == "pending_admin" and len(tk.messages) == 1
    m = tk.messages[0]
    assert m.sender_type == "user" and m.attachment_path
    # The bytes actually landed on disk under the tickets root.
    assert ticket_service.resolve_attachment_path(m.attachment_path) is not None


async def test_status_transitions_on_replies(db_session) -> None:
    u = await _user(db_session)
    tk = await ticket_service.create_ticket(db_session, u.id, "s", "hi")
    await db_session.commit()
    await ticket_service.add_admin_reply(db_session, tk.id, 7, "looking")
    await db_session.commit()
    assert (await ticket_service.get_ticket(db_session, tk.id)).status == "pending_user"
    await ticket_service.add_user_reply(db_session, tk.id, u.id, "thanks")
    await db_session.commit()
    tk2 = await ticket_service.get_ticket(db_session, tk.id)
    assert tk2.status == "pending_admin" and len(tk2.messages) == 3


async def test_user_cannot_reply_to_another_users_ticket(db_session) -> None:
    u = await _user(db_session, tg=1)
    other = await _user(db_session, tg=2)
    tk = await ticket_service.create_ticket(db_session, u.id, "s", "hi")
    await db_session.commit()
    with pytest.raises(TicketError) as exc:
        await ticket_service.add_user_reply(db_session, tk.id, other.id, "hax")
    assert exc.value.code == "not_your_ticket"


async def test_close_and_reopen(db_session) -> None:
    u = await _user(db_session)
    tk = await ticket_service.create_ticket(db_session, u.id, "s", "hi")
    await db_session.commit()
    await ticket_service.close_ticket(db_session, tk.id, actor_id=u.id, actor_type="user",
                                      user_id=u.id)
    await db_session.commit()
    closed = await ticket_service.get_ticket(db_session, tk.id)
    assert closed.status == "closed" and closed.closed_at is not None
    # A closed ticket cannot receive a user reply until reopened.
    with pytest.raises(TicketError):
        await ticket_service.add_user_reply(db_session, tk.id, u.id, "more")
    await ticket_service.reopen_ticket(db_session, tk.id, u.id)
    await db_session.commit()
    assert (await ticket_service.get_ticket(db_session, tk.id)).status == "pending_admin"


async def test_reopen_blocked_when_setting_off(db_session) -> None:
    from app.core.settings_service import SettingsService
    u = await _user(db_session)
    tk = await ticket_service.create_ticket(db_session, u.id, "s", "hi")
    await db_session.commit()
    await ticket_service.close_ticket(db_session, tk.id, actor_id=None, actor_type="admin")
    await db_session.commit()
    await SettingsService(db_session).set("allow_reopen_closed_tickets", False)
    with pytest.raises(TicketError) as exc:
        await ticket_service.reopen_ticket(db_session, tk.id, u.id)
    assert exc.value.code == "reopen_disabled"


async def test_assign_and_priority(db_session) -> None:
    u = await _user(db_session)
    tk = await ticket_service.create_ticket(db_session, u.id, "s", "hi")
    await db_session.commit()
    await ticket_service.assign_ticket(db_session, tk.id, 5)
    await ticket_service.set_priority(db_session, tk.id, "urgent", admin_id=5)
    await db_session.commit()
    tk2 = await ticket_service.get_ticket(db_session, tk.id)
    assert tk2.assigned_admin_id == 5 and tk2.priority == "urgent"
    with pytest.raises(TicketError):
        await ticket_service.set_priority(db_session, tk.id, "bogus", admin_id=5)


async def test_attachment_type_and_size_validation(db_session) -> None:
    with pytest.raises(TicketError) as exc:
        await ticket_service.validate_attachment(db_session, "evil.exe", 100,
                                                 "application/x-msdownload")
    assert exc.value.code == "unsupported_type"
    # 11 MB > default 10 MB.
    with pytest.raises(TicketError) as exc2:
        await ticket_service.validate_attachment(db_session, "big.png", 11 * 1024 * 1024,
                                                 "image/png")
    assert exc2.value.code == "too_large"


async def test_audit_rows_written(db_session) -> None:
    u = await _user(db_session)
    tk = await ticket_service.create_ticket(db_session, u.id, "s", "hi")
    await db_session.commit()
    await ticket_service.add_admin_reply(db_session, tk.id, 7, "hello")
    await ticket_service.add_user_reply(db_session, tk.id, u.id, "hi back")
    await ticket_service.close_ticket(db_session, tk.id, actor_id=7, actor_type="admin")
    await db_session.commit()
    actions = {r.action for r in (await db_session.execute(
        select(AuditLog).where(AuditLog.target_type == "ticket"))).scalars().all()}
    assert {"ticket_created", "ticket_admin_replied", "ticket_user_replied",
            "ticket_closed"} <= actions


async def test_list_user_tickets_only_own(db_session) -> None:
    a = await _user(db_session, tg=1)
    b = await _user(db_session, tg=2)
    await ticket_service.create_ticket(db_session, a.id, "a1", "x")
    await ticket_service.create_ticket(db_session, b.id, "b1", "y")
    await db_session.commit()
    a_list = await ticket_service.list_user_tickets(db_session, a.id)
    assert len(a_list) == 1 and a_list[0].user_id == a.id

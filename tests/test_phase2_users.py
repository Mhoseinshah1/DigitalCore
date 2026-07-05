"""Phase 2 user service: registration, block/unblock, wallet, verify, stats."""
from __future__ import annotations

import pytest
from sqlalchemy import select

from app.models import AuditLog, User, WalletTransaction
from app.services import user_service


async def _mk_user(session, tid=1001, **kw) -> User:
    user, _ = await user_service.create_or_update_from_telegram(
        session, telegram_id=tid, username=kw.get("username", "u"),
        first_name=kw.get("first_name", "F"), language_code=kw.get("language_code"),
    )
    return user


async def test_create_or_update_captures_language_code(db_session) -> None:
    user, created = await user_service.create_or_update_from_telegram(
        db_session, telegram_id=42, username="neo", first_name="Neo", language_code="en"
    )
    assert created is True
    assert user.language_code == "en"
    assert user.last_activity_at is not None
    # Second call updates, does not duplicate.
    user2, created2 = await user_service.create_or_update_from_telegram(
        db_session, telegram_id=42, username="neo2", first_name="Neo"
    )
    assert created2 is False and user2.id == user.id and user2.username == "neo2"


async def test_block_unblock_writes_audit(db_session) -> None:
    user = await _mk_user(db_session, tid=2001)
    await user_service.admin_set_blocked(db_session, user.id, True, actor_id=7)
    await db_session.commit()
    assert (await user_service.get_by_id(db_session, user.id)).is_blocked is True

    blocked = await user_service.list_blocked_users(db_session)
    assert user.id in {u.id for u in blocked}

    await user_service.admin_set_blocked(db_session, user.id, False, actor_id=7)
    await db_session.commit()
    assert (await user_service.get_by_id(db_session, user.id)).is_blocked is False

    actions = [
        r.action for r in (
            await db_session.execute(
                select(AuditLog).where(AuditLog.target_type == "user").order_by(AuditLog.id)
            )
        ).scalars().all()
    ]
    assert "user.blocked" in actions and "user.unblocked" in actions


async def test_wallet_credit_and_debit_with_ledger_and_audit(db_session) -> None:
    user = await _mk_user(db_session, tid=3001)
    user = await user_service.adjust_wallet_balance(
        db_session, user.id, 100_000, reason="topup", actor_id=1
    )
    assert user.wallet_balance == 100_000
    user = await user_service.adjust_wallet_balance(
        db_session, user.id, -30_000, reason="refund-reversal", actor_id=1
    )
    assert user.wallet_balance == 70_000

    txns = await user_service.list_wallet_transactions(db_session, user_id=user.id)
    assert [t.amount for t in txns] == [-30_000, 100_000]  # newest first
    assert txns[0].balance_after == 70_000

    audits = (
        await db_session.execute(
            select(AuditLog).where(AuditLog.action == "wallet.adjusted")
        )
    ).scalars().all()
    assert len(audits) == 2


async def test_wallet_cannot_go_negative(db_session) -> None:
    user = await _mk_user(db_session, tid=4001)
    with pytest.raises(ValueError):
        await user_service.adjust_wallet_balance(db_session, user.id, -5000)
    # No ledger row, no balance change on the rejected debit.
    assert (await user_service.get_by_id(db_session, user.id)).wallet_balance == 0
    assert (await db_session.execute(select(WalletTransaction))).scalars().all() == []


async def test_wallet_allow_negative_flag(db_session) -> None:
    user = await _mk_user(db_session, tid=4002)
    user = await user_service.adjust_wallet_balance(
        db_session, user.id, -5000, allow_negative=True
    )
    assert user.wallet_balance == -5000


async def test_wallet_zero_amount_rejected(db_session) -> None:
    user = await _mk_user(db_session, tid=4003)
    with pytest.raises(ValueError):
        await user_service.adjust_wallet_balance(db_session, user.id, 0)


async def test_verify_and_note_and_stats(db_session) -> None:
    user = await _mk_user(db_session, tid=5001)
    await user_service.set_verified(db_session, user.id, True, actor_id=1)
    await user_service.update_admin_note(db_session, user.id, "vip", actor_id=1)
    await db_session.commit()
    refreshed = await user_service.get_by_id(db_session, user.id)
    assert refreshed.is_verified is True and refreshed.admin_note == "vip"

    await _mk_user(db_session, tid=5002)
    await user_service.admin_set_blocked(db_session, refreshed.id, True, actor_id=1)
    await db_session.commit()
    stats = await user_service.get_stats(db_session)
    assert stats["total_users"] == 2
    assert stats["blocked_users"] == 1
    assert stats["verified_users"] == 1


async def test_get_user_summary_shape(db_session) -> None:
    user = await _mk_user(db_session, tid=6001, first_name="Ada", username="ada")
    summary = user_service.get_user_summary(user)
    assert summary["telegram_id"] == 6001
    assert summary["username"] == "ada"
    assert summary["wallet_balance"] == 0
    assert summary["is_blocked"] is False

"""Admin wallet adjustments (Phase 4).

A thin, transactional wrapper over `user_service.adjust_wallet_balance` that adds
the add/subtract semantics used by the receipt-review quick actions. Every call
records a `WalletTransaction` (with `balance_before`/`balance_after`/`type`/
`actor_id`/`reason`) and an audit row. Negative balances are refused unless the
`allow_negative_wallet` setting is on.

This is manual admin adjustment only — there is no wallet *purchase* flow.
"""
from __future__ import annotations

from sqlalchemy.ext.asyncio import AsyncSession

from app.core.settings_service import SettingsService
from app.models.user import User
from app.services import user_service


async def _allow_negative(session: AsyncSession) -> bool:
    return await SettingsService(session).get_bool("allow_negative_wallet", False)


async def adjust_balance(
    session: AsyncSession,
    user_id: int,
    amount: int,
    *,
    admin_id: int | None,
    reason: str,
    transaction_type: str = "admin_adjustment",
    ip_address: str | None = None,
) -> User:
    """Signed adjustment (+credit / -debit). `reason` is required and non-empty."""
    if not (reason or "").strip():
        raise ValueError("a reason is required")
    return await user_service.adjust_wallet_balance(
        session, user_id, int(amount),
        reason=reason.strip(), actor_type="admin", actor_id=admin_id,
        allow_negative=await _allow_negative(session),
        transaction_type=transaction_type, ip_address=ip_address,
    )


async def add_balance(
    session: AsyncSession, user_id: int, amount: int, *,
    admin_id: int | None, reason: str, ip_address: str | None = None,
) -> User:
    if int(amount) <= 0:
        raise ValueError("amount must be a positive number")
    return await adjust_balance(
        session, user_id, int(amount), admin_id=admin_id, reason=reason,
        ip_address=ip_address,
    )


async def subtract_balance(
    session: AsyncSession, user_id: int, amount: int, *,
    admin_id: int | None, reason: str, ip_address: str | None = None,
) -> User:
    if int(amount) <= 0:
        raise ValueError("amount must be a positive number")
    # adjust_balance applies the `allow_negative_wallet` guard.
    return await adjust_balance(
        session, user_id, -int(amount), admin_id=admin_id, reason=reason,
        ip_address=ip_address,
    )

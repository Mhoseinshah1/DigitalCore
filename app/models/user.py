"""End users (Telegram bot users)."""
from __future__ import annotations

from datetime import datetime

from sqlalchemy import BigInteger, Boolean, DateTime, ForeignKey, String, Text
from sqlalchemy.orm import Mapped, mapped_column

from app.models.base import Base, TimestampMixin


class User(Base, TimestampMixin):
    __tablename__ = "users"

    id: Mapped[int] = mapped_column(primary_key=True)
    telegram_id: Mapped[int | None] = mapped_column(
        BigInteger, unique=True, index=True, nullable=True
    )
    username: Mapped[str | None] = mapped_column(String(255), nullable=True)
    first_name: Mapped[str | None] = mapped_column(String(255), nullable=True)
    last_name: Mapped[str | None] = mapped_column(String(255), nullable=True)
    phone_number: Mapped[str | None] = mapped_column(String(32), nullable=True)

    # Raw Telegram language_code (e.g. "en", "fa", "ru"); distinct from the UI
    # `language` preference below.
    language_code: Mapped[str | None] = mapped_column(String(16), nullable=True)

    is_blocked: Mapped[bool] = mapped_column(Boolean, default=False, nullable=False)
    is_verified: Mapped[bool] = mapped_column(Boolean, default=False, nullable=False)

    # Restriction is softer than a block: the user can still open the bot and
    # reach support/rules, but cannot order/buy/submit receipts/top up.
    is_restricted: Mapped[bool] = mapped_column(
        Boolean, default=False, server_default="0", nullable=False
    )
    restriction_reason: Mapped[str | None] = mapped_column(Text, nullable=True)
    restricted_until: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True), nullable=True
    )

    # Wallet balance in integer toman (the platform's money convention). A
    # dedicated wallet_transactions row records every change; no purchase logic
    # in this phase.
    wallet_balance: Mapped[int] = mapped_column(
        BigInteger, default=0, server_default="0", nullable=False
    )

    # Free-text note visible only to admins.
    admin_note: Mapped[str | None] = mapped_column(Text, nullable=True)

    # UI language (fa/en); fa is the platform default.
    language: Mapped[str] = mapped_column(
        String(5), default="fa", server_default="fa", nullable=False
    )

    # Self-referential: the user who invited this one. Set once, on the first
    # /start carrying a valid referral code (Phase 10); never overwritten.
    referrer_id: Mapped[int | None] = mapped_column(
        ForeignKey("users.id", name="fk_users_referrer_id_users"), nullable=True
    )
    # This user's own shareable referral code (Phase 10), and when a referrer was
    # first attached to them.
    referral_code: Mapped[str | None] = mapped_column(
        String(32), unique=True, index=True, nullable=True
    )
    referral_registered_at: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True), nullable=True
    )

    last_activity_at: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True), nullable=True
    )

    def __repr__(self) -> str:  # pragma: no cover - debug helper
        return f"<User id={self.id} telegram_id={self.telegram_id}>"

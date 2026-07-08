"""Admin-configurable payment methods (Payment Core slice 1).

Each row is one way a user can pay: wallet, manual card-to-card receipt, or a
future gateway (custom/online/crypto/telegram stars). Gateway credentials are
stored Fernet-encrypted (app/core/crypto.py) and are never rendered in any UI
or log. Amount limits and `activate_after_payments` gate who sees a method;
`cashback_percent` funds top-up bonuses. Six defaults are seeded by migration
0021 — only wallet and manual_receipt start active.
"""
from __future__ import annotations

from sqlalchemy import BigInteger, Boolean, Integer, Numeric, String, Text
from sqlalchemy.orm import Mapped, mapped_column

from app.models.base import Base, TimestampMixin

PAYMENT_METHOD_TYPES: tuple[str, ...] = (
    "wallet", "manual_receipt", "custom_gateway", "online_gateway",
    "crypto", "telegram_stars",
)


class PaymentMethod(Base, TimestampMixin):
    __tablename__ = "payment_methods"

    id: Mapped[int] = mapped_column(primary_key=True)
    code: Mapped[str] = mapped_column(String(40), nullable=False, unique=True, index=True)
    title: Mapped[str] = mapped_column(String(120), nullable=False)
    method_type: Mapped[str] = mapped_column(String(20), nullable=False)

    is_active: Mapped[bool] = mapped_column(
        Boolean, nullable=False, default=True, server_default="1"
    )
    sort_order: Mapped[int] = mapped_column(
        Integer, nullable=False, default=0, server_default="0"
    )

    # Integer toman; null/0 = no limit.
    min_amount: Mapped[int | None] = mapped_column(BigInteger, nullable=True)
    max_amount: Mapped[int | None] = mapped_column(BigInteger, nullable=True)

    # Top-up bonus percentage (e.g. 5 = +5% wallet credit on approval).
    cashback_percent: Mapped[float] = mapped_column(
        Numeric(5, 2), nullable=False, default=0, server_default="0"
    )
    # Show this method only after the user has N approved payments (0 = always).
    activate_after_payments: Mapped[int] = mapped_column(
        Integer, nullable=False, default=0, server_default="0"
    )

    # Gateway plumbing (future phases). Secrets are Fernet ciphertext at rest and
    # are NEVER surfaced in the admin UI, exports, or logs.
    api_url: Mapped[str | None] = mapped_column(String(255), nullable=True)
    api_token_encrypted: Mapped[str | None] = mapped_column(Text, nullable=True)
    merchant_id_encrypted: Mapped[str | None] = mapped_column(Text, nullable=True)

    # Persian instructions shown to the user when this method is chosen.
    instruction_text: Mapped[str | None] = mapped_column(Text, nullable=True)

    def __repr__(self) -> str:  # pragma: no cover - debug helper
        return f"<PaymentMethod id={self.id} code={self.code!r} active={self.is_active}>"

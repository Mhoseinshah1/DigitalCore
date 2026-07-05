"""phase 2: user wallet/note/language_code, audit meta/ip, wallet_transactions,
settings key reconciliation

Schema:
  - users: add language_code, wallet_balance (default 0), admin_note
  - audit_logs: add meta, ip_address
  - create wallet_transactions
Data:
  - rename legacy settings keys to their Phase 2 canonical names, preserving any
    operator-entered values (only when the canonical key does not already exist).

Works on a fresh database and an existing one. Uses batch_alter_table so the
column adds also apply cleanly under SQLite.

Revision ID: 0008_phase2_admin_foundation
Revises: 0007_xui_servers
Create Date: 2025-01-08 00:00:00
"""
from alembic import op
import sqlalchemy as sa

revision = "0008_phase2_admin_foundation"
down_revision = "0007_xui_servers"
branch_labels = None
depends_on = None

# (legacy key -> Phase 2 canonical key). Values are preserved.
_SETTING_RENAMES = (
    ("sheba", "sheba_number"),
    ("support_admin_username", "support_username"),
    ("card_payment_enabled", "card_to_card_enabled"),
    ("success_purchase_text", "successful_purchase_text"),
    # The old payment_text held the card-to-card *instructions*; it becomes the
    # payment settings field. A new bot-text `payment_text` is seeded separately.
    ("payment_text", "payment_instructions"),
)


def upgrade() -> None:
    with op.batch_alter_table("users") as batch:
        batch.add_column(sa.Column("language_code", sa.String(length=16), nullable=True))
        batch.add_column(
            sa.Column(
                "wallet_balance",
                sa.BigInteger(),
                nullable=False,
                server_default="0",
            )
        )
        batch.add_column(sa.Column("admin_note", sa.Text(), nullable=True))

    with op.batch_alter_table("audit_logs") as batch:
        batch.add_column(sa.Column("meta", sa.Text(), nullable=True))
        batch.add_column(sa.Column("ip_address", sa.String(length=64), nullable=True))

    op.create_table(
        "wallet_transactions",
        sa.Column("id", sa.Integer(), primary_key=True),
        sa.Column(
            "user_id",
            sa.Integer(),
            sa.ForeignKey("users.id", ondelete="CASCADE"),
            nullable=False,
        ),
        sa.Column("amount", sa.BigInteger(), nullable=False),
        sa.Column("balance_after", sa.BigInteger(), nullable=False),
        sa.Column("reason", sa.String(length=255), nullable=True),
        sa.Column("actor_type", sa.String(length=16), nullable=False, server_default="admin"),
        sa.Column("actor_id", sa.BigInteger(), nullable=True),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            server_default=sa.func.now(),
            nullable=False,
        ),
    )
    op.create_index("ix_wallet_transactions_user_id", "wallet_transactions", ["user_id"])
    op.create_index("ix_wallet_transactions_created_at", "wallet_transactions", ["created_at"])

    for old, new in _SETTING_RENAMES:
        op.execute(
            f"UPDATE settings SET key = '{new}' "
            f"WHERE key = '{old}' "
            f"AND NOT EXISTS (SELECT 1 FROM settings s2 WHERE s2.key = '{new}')"
        )
        op.execute(f"DELETE FROM settings WHERE key = '{old}'")


def downgrade() -> None:
    for old, new in _SETTING_RENAMES:
        op.execute(
            f"UPDATE settings SET key = '{old}' "
            f"WHERE key = '{new}' "
            f"AND NOT EXISTS (SELECT 1 FROM settings s2 WHERE s2.key = '{old}')"
        )
        op.execute(f"DELETE FROM settings WHERE key = '{new}'")

    op.drop_index("ix_wallet_transactions_created_at", table_name="wallet_transactions")
    op.drop_index("ix_wallet_transactions_user_id", table_name="wallet_transactions")
    op.drop_table("wallet_transactions")

    with op.batch_alter_table("audit_logs") as batch:
        batch.drop_column("ip_address")
        batch.drop_column("meta")

    with op.batch_alter_table("users") as batch:
        batch.drop_column("admin_note")
        batch.drop_column("wallet_balance")
        batch.drop_column("language_code")

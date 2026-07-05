"""phase 4: user restriction, wallet balance_before/type, order delivered_payload,
license_keys pool

- users: is_restricted (default false), restriction_reason, restricted_until
- wallet_transactions: balance_before (default 0), type (default admin_adjustment)
- orders: delivered_payload (delivered license code / v2ray credentials)
- license_keys: a simple per-product key pool consumed on license delivery

Additive only — runs identically on a fresh and an existing database.

Revision ID: 0011_phase4_actions
Revises: 0010_orders_payments
Create Date: 2025-01-11 00:00:00
"""
from alembic import op
import sqlalchemy as sa

revision = "0011_phase4_actions"
down_revision = "0010_orders_payments"
branch_labels = None
depends_on = None


def upgrade() -> None:
    with op.batch_alter_table("users") as batch:
        batch.add_column(sa.Column("is_restricted", sa.Boolean(), nullable=False,
                                   server_default=sa.false()))
        batch.add_column(sa.Column("restriction_reason", sa.Text(), nullable=True))
        batch.add_column(sa.Column("restricted_until", sa.DateTime(timezone=True), nullable=True))

    with op.batch_alter_table("wallet_transactions") as batch:
        batch.add_column(sa.Column("balance_before", sa.BigInteger(), nullable=False,
                                   server_default="0"))
        batch.add_column(sa.Column("type", sa.String(length=32), nullable=False,
                                   server_default="admin_adjustment"))

    with op.batch_alter_table("orders") as batch:
        batch.add_column(sa.Column("delivered_payload", sa.Text(), nullable=True))

    op.create_table(
        "license_keys",
        sa.Column("id", sa.Integer(), primary_key=True),
        sa.Column("product_id", sa.Integer(),
                  sa.ForeignKey("products.id", ondelete="CASCADE"), nullable=False),
        sa.Column("code", sa.String(length=255), nullable=False),
        sa.Column("is_used", sa.Boolean(), nullable=False, server_default=sa.false()),
        sa.Column("order_id", sa.Integer(),
                  sa.ForeignKey("orders.id", ondelete="SET NULL"), nullable=True),
        sa.Column("created_at", sa.DateTime(timezone=True),
                  server_default=sa.func.now(), nullable=False),
        sa.Column("used_at", sa.DateTime(timezone=True), nullable=True),
        sa.UniqueConstraint("product_id", "code", name="uq_license_keys_product_code"),
    )
    op.create_index("ix_license_keys_product_id", "license_keys", ["product_id"])
    op.create_index("ix_license_keys_is_used", "license_keys", ["is_used"])


def downgrade() -> None:
    op.drop_index("ix_license_keys_is_used", table_name="license_keys")
    op.drop_index("ix_license_keys_product_id", table_name="license_keys")
    op.drop_table("license_keys")

    with op.batch_alter_table("orders") as batch:
        batch.drop_column("delivered_payload")

    with op.batch_alter_table("wallet_transactions") as batch:
        batch.drop_column("type")
        batch.drop_column("balance_before")

    with op.batch_alter_table("users") as batch:
        batch.drop_column("restricted_until")
        batch.drop_column("restriction_reason")
        batch.drop_column("is_restricted")

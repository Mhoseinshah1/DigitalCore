"""phase 7: wallet top-up requests + wallet-transaction links + refund fields

Additive: creates wallet_topup_requests, links wallet_transactions to their
cause (order/payment/topup) with a status, and adds refund fields to payments
and orders. Runs on a fresh and an existing database.

Revision ID: 0014_wallet_topups
Revises: 0013_v2ray_services
Create Date: 2025-01-28 00:00:00
"""
from alembic import op
import sqlalchemy as sa

revision = "0014_wallet_topups"
down_revision = "0013_v2ray_services"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.create_table(
        "wallet_topup_requests",
        sa.Column("id", sa.Integer(), primary_key=True),
        sa.Column("user_id", sa.Integer(),
                  sa.ForeignKey("users.id", ondelete="CASCADE"), nullable=False),
        sa.Column("amount", sa.BigInteger(), nullable=False, server_default="0"),
        sa.Column("status", sa.String(length=20), nullable=False,
                  server_default="pending_receipt"),
        sa.Column("receipt_file_id", sa.String(length=255), nullable=True),
        sa.Column("receipt_path", sa.String(length=512), nullable=True),
        sa.Column("receipt_mime_type", sa.String(length=128), nullable=True),
        sa.Column("receipt_original_name", sa.String(length=255), nullable=True),
        sa.Column("receipt_size", sa.Integer(), nullable=True),
        sa.Column("admin_id", sa.Integer(),
                  sa.ForeignKey("admins.id", ondelete="SET NULL"), nullable=True),
        sa.Column("reject_reason", sa.Text(), nullable=True),
        sa.Column("submitted_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("approved_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("rejected_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("created_at", sa.DateTime(timezone=True),
                  server_default=sa.func.now(), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True),
                  server_default=sa.func.now(), nullable=False),
    )
    op.create_index("ix_wallet_topup_requests_user_id", "wallet_topup_requests", ["user_id"])
    op.create_index("ix_wallet_topup_requests_status", "wallet_topup_requests", ["status"])

    # Link wallet transactions to their cause + carry a status.
    with op.batch_alter_table("wallet_transactions") as batch:
        batch.add_column(sa.Column("status", sa.String(length=16), nullable=False,
                                   server_default="completed"))
        batch.add_column(sa.Column("order_id", sa.Integer(), nullable=True))
        batch.add_column(sa.Column("payment_id", sa.Integer(), nullable=True))
        batch.add_column(sa.Column("topup_id", sa.Integer(), nullable=True))
        batch.create_foreign_key("fk_wallet_tx_order", "orders", ["order_id"], ["id"],
                                 ondelete="SET NULL")
        batch.create_foreign_key("fk_wallet_tx_payment", "payments", ["payment_id"], ["id"],
                                 ondelete="SET NULL")
        batch.create_foreign_key("fk_wallet_tx_topup", "wallet_topup_requests",
                                 ["topup_id"], ["id"], ondelete="SET NULL")
    op.create_index("ix_wallet_transactions_order_id", "wallet_transactions", ["order_id"])

    # Refund foundation.
    with op.batch_alter_table("payments") as batch:
        batch.add_column(sa.Column("refunded_at", sa.DateTime(timezone=True), nullable=True))
        batch.add_column(sa.Column("refunded_amount", sa.BigInteger(), nullable=False,
                                   server_default="0"))
    with op.batch_alter_table("orders") as batch:
        batch.add_column(sa.Column("refunded_at", sa.DateTime(timezone=True), nullable=True))
        batch.add_column(sa.Column("refund_reason", sa.Text(), nullable=True))


def downgrade() -> None:
    with op.batch_alter_table("orders") as batch:
        batch.drop_column("refund_reason")
        batch.drop_column("refunded_at")
    with op.batch_alter_table("payments") as batch:
        batch.drop_column("refunded_amount")
        batch.drop_column("refunded_at")

    op.drop_index("ix_wallet_transactions_order_id", table_name="wallet_transactions")
    with op.batch_alter_table("wallet_transactions") as batch:
        batch.drop_constraint("fk_wallet_tx_topup", type_="foreignkey")
        batch.drop_constraint("fk_wallet_tx_payment", type_="foreignkey")
        batch.drop_constraint("fk_wallet_tx_order", type_="foreignkey")
        batch.drop_column("topup_id")
        batch.drop_column("payment_id")
        batch.drop_column("order_id")
        batch.drop_column("status")

    op.drop_index("ix_wallet_topup_requests_status", table_name="wallet_topup_requests")
    op.drop_index("ix_wallet_topup_requests_user_id", table_name="wallet_topup_requests")
    op.drop_table("wallet_topup_requests")

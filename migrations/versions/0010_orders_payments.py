"""phase 3: orders + payments (card-to-card receipt flow)

Adds the `orders` and `payments` tables. Orders reference users/products (and,
later, an acting admin); payments hold the money side + receipt metadata. No
data backfill is needed, so this runs identically on a fresh and an existing
database.

Revision ID: 0010_orders_payments
Revises: 0009_xui_binding
Create Date: 2025-01-10 00:00:00
"""
from alembic import op
import sqlalchemy as sa

revision = "0010_orders_payments"
down_revision = "0009_xui_binding"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.create_table(
        "orders",
        sa.Column("id", sa.Integer(), primary_key=True),
        sa.Column("order_number", sa.String(length=40), nullable=False),
        sa.Column(
            "user_id", sa.Integer(),
            sa.ForeignKey("users.id", ondelete="CASCADE"), nullable=False,
        ),
        sa.Column(
            "product_id", sa.Integer(),
            sa.ForeignKey("products.id", ondelete="RESTRICT"), nullable=False,
        ),
        sa.Column("amount", sa.BigInteger(), nullable=False, server_default="0"),
        sa.Column("discount_amount", sa.BigInteger(), nullable=False, server_default="0"),
        sa.Column("final_amount", sa.BigInteger(), nullable=False, server_default="0"),
        sa.Column("status", sa.String(length=32), nullable=False, server_default="pending_payment"),
        sa.Column("payment_method", sa.String(length=16), nullable=False, server_default="card_to_card"),
        sa.Column(
            "admin_id", sa.Integer(),
            sa.ForeignKey("admins.id", ondelete="SET NULL"), nullable=True,
        ),
        sa.Column("reject_reason", sa.Text(), nullable=True),
        sa.Column("user_note", sa.Text(), nullable=True),
        sa.Column("created_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False),
        sa.Column("paid_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("approved_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("rejected_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("cancelled_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("delivered_at", sa.DateTime(timezone=True), nullable=True),
        sa.UniqueConstraint("order_number", name="uq_orders_order_number"),
    )
    op.create_index("ix_orders_order_number", "orders", ["order_number"])
    op.create_index("ix_orders_user_id", "orders", ["user_id"])
    op.create_index("ix_orders_product_id", "orders", ["product_id"])
    op.create_index("ix_orders_status", "orders", ["status"])

    op.create_table(
        "payments",
        sa.Column("id", sa.Integer(), primary_key=True),
        sa.Column(
            "order_id", sa.Integer(),
            sa.ForeignKey("orders.id", ondelete="CASCADE"), nullable=False,
        ),
        sa.Column(
            "user_id", sa.Integer(),
            sa.ForeignKey("users.id", ondelete="CASCADE"), nullable=False,
        ),
        sa.Column("amount", sa.BigInteger(), nullable=False, server_default="0"),
        sa.Column("method", sa.String(length=16), nullable=False, server_default="card_to_card"),
        sa.Column("status", sa.String(length=32), nullable=False, server_default="pending"),
        sa.Column("tracking_code", sa.String(length=120), nullable=True),
        sa.Column("receipt_file_id", sa.String(length=255), nullable=True),
        sa.Column("receipt_path", sa.String(length=512), nullable=True),
        sa.Column("receipt_mime_type", sa.String(length=128), nullable=True),
        sa.Column("receipt_original_name", sa.String(length=255), nullable=True),
        sa.Column("receipt_size", sa.Integer(), nullable=True),
        sa.Column(
            "admin_id", sa.Integer(),
            sa.ForeignKey("admins.id", ondelete="SET NULL"), nullable=True,
        ),
        sa.Column("created_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False),
        sa.Column("submitted_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("approved_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("rejected_at", sa.DateTime(timezone=True), nullable=True),
    )
    op.create_index("ix_payments_order_id", "payments", ["order_id"])
    op.create_index("ix_payments_user_id", "payments", ["user_id"])
    op.create_index("ix_payments_status", "payments", ["status"])


def downgrade() -> None:
    op.drop_index("ix_payments_status", table_name="payments")
    op.drop_index("ix_payments_user_id", table_name="payments")
    op.drop_index("ix_payments_order_id", table_name="payments")
    op.drop_table("payments")

    op.drop_index("ix_orders_status", table_name="orders")
    op.drop_index("ix_orders_product_id", table_name="orders")
    op.drop_index("ix_orders_user_id", table_name="orders")
    op.drop_index("ix_orders_order_number", table_name="orders")
    op.drop_table("orders")

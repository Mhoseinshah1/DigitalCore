"""products table (license | v2ray definitions; no stock/orders yet)

Revision ID: 0006_products
Revises: 0005_user_language
Create Date: 2025-01-06 00:00:00
"""
from alembic import op
import sqlalchemy as sa

revision = "0006_products"
down_revision = "0005_user_language"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.create_table(
        "products",
        sa.Column("id", sa.Integer(), primary_key=True),
        sa.Column("type", sa.String(length=16), nullable=False),
        sa.Column("title", sa.String(length=200), nullable=False),
        sa.Column("description", sa.Text(), nullable=True),
        sa.Column("price", sa.BigInteger(), nullable=False, server_default="0"),
        sa.Column("duration_days", sa.Integer(), nullable=True),
        sa.Column("traffic_gb", sa.Integer(), nullable=True),
        sa.Column("ip_limit", sa.Integer(), nullable=True),
        sa.Column("server_id", sa.Integer(), nullable=True),
        sa.Column("inbound_id", sa.Integer(), nullable=True),
        sa.Column("is_active", sa.Boolean(), nullable=False, server_default=sa.true()),
        sa.Column("is_hidden", sa.Boolean(), nullable=False, server_default=sa.false()),
        sa.Column("stock_count", sa.Integer(), nullable=False, server_default="0"),
        sa.Column("sort_order", sa.Integer(), nullable=False, server_default="0"),
        sa.Column("created_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False),
    )
    op.create_index("ix_products_type", "products", ["type"])
    op.create_index("ix_products_active_hidden", "products", ["is_active", "is_hidden"])


def downgrade() -> None:
    op.drop_index("ix_products_active_hidden", table_name="products")
    op.drop_index("ix_products_type", table_name="products")
    op.drop_table("products")

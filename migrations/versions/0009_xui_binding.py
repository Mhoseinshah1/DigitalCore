"""phase 2.1: xui server is_active/last_error, inbound network/security, product
xui_server_id/xui_inbound_id bindings

Schema:
  - xui_servers: add is_active (default true), last_error; relax username +
    encrypted_password to nullable (a server may authenticate by API token only)
  - xui_inbounds: add network, security
  - products: add xui_server_id (FK xui_servers), xui_inbound_id (FK xui_inbounds)

Works on a fresh database and an existing one. batch_alter_table keeps the
column adds SQLite-safe.

Revision ID: 0009_xui_binding
Revises: 0008_phase2_admin_foundation
Create Date: 2025-01-09 00:00:00
"""
from alembic import op
import sqlalchemy as sa

revision = "0009_xui_binding"
down_revision = "0008_phase2_admin_foundation"
branch_labels = None
depends_on = None


def upgrade() -> None:
    with op.batch_alter_table("xui_servers") as batch:
        batch.add_column(
            sa.Column("is_active", sa.Boolean(), nullable=False, server_default="1")
        )
        batch.add_column(sa.Column("last_error", sa.Text(), nullable=True))
        batch.alter_column("username", existing_type=sa.String(length=120), nullable=True)
        batch.alter_column("encrypted_password", existing_type=sa.Text(), nullable=True)

    with op.batch_alter_table("xui_inbounds") as batch:
        batch.add_column(sa.Column("network", sa.String(length=32), nullable=True))
        batch.add_column(sa.Column("security", sa.String(length=32), nullable=True))

    with op.batch_alter_table("products") as batch:
        batch.add_column(sa.Column("xui_server_id", sa.Integer(), nullable=True))
        batch.add_column(sa.Column("xui_inbound_id", sa.Integer(), nullable=True))
        batch.create_foreign_key(
            "fk_products_xui_server_id", "xui_servers", ["xui_server_id"], ["id"],
            ondelete="SET NULL",
        )
        batch.create_foreign_key(
            "fk_products_xui_inbound_id", "xui_inbounds", ["xui_inbound_id"], ["id"],
            ondelete="SET NULL",
        )
    op.create_index("ix_products_xui_server_id", "products", ["xui_server_id"])
    op.create_index("ix_products_xui_inbound_id", "products", ["xui_inbound_id"])


def downgrade() -> None:
    op.drop_index("ix_products_xui_inbound_id", table_name="products")
    op.drop_index("ix_products_xui_server_id", table_name="products")
    with op.batch_alter_table("products") as batch:
        batch.drop_constraint("fk_products_xui_inbound_id", type_="foreignkey")
        batch.drop_constraint("fk_products_xui_server_id", type_="foreignkey")
        batch.drop_column("xui_inbound_id")
        batch.drop_column("xui_server_id")

    with op.batch_alter_table("xui_inbounds") as batch:
        batch.drop_column("security")
        batch.drop_column("network")

    with op.batch_alter_table("xui_servers") as batch:
        batch.alter_column("encrypted_password", existing_type=sa.Text(), nullable=False)
        batch.alter_column("username", existing_type=sa.String(length=120), nullable=False)
        batch.drop_column("last_error")
        batch.drop_column("is_active")

"""phase 6: v2ray_services table (real 3X-UI provisioning)

Additive: one row per provisioned v2ray order. Runs on a fresh and an existing
database (pure create_table + create_index, no alterations to existing tables).

Revision ID: 0013_v2ray_services
Revises: 0012_license_items
Create Date: 2025-01-20 00:00:00
"""
from alembic import op
import sqlalchemy as sa

revision = "0013_v2ray_services"
down_revision = "0012_license_items"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.create_table(
        "v2ray_services",
        sa.Column("id", sa.Integer(), primary_key=True),
        sa.Column("user_id", sa.Integer(),
                  sa.ForeignKey("users.id", ondelete="CASCADE"), nullable=False),
        sa.Column("order_id", sa.Integer(),
                  sa.ForeignKey("orders.id", ondelete="CASCADE"), nullable=False),
        sa.Column("product_id", sa.Integer(),
                  sa.ForeignKey("products.id", ondelete="RESTRICT"), nullable=False),
        sa.Column("xui_server_id", sa.Integer(),
                  sa.ForeignKey("xui_servers.id", ondelete="RESTRICT"), nullable=False),
        sa.Column("xui_inbound_id", sa.Integer(),
                  sa.ForeignKey("xui_inbounds.id", ondelete="RESTRICT"), nullable=False),
        sa.Column("client_email", sa.String(length=255), nullable=False),
        sa.Column("client_uuid", sa.String(length=64), nullable=False),
        sa.Column("sub_id", sa.String(length=64), nullable=True),
        sa.Column("subscription_url", sa.Text(), nullable=True),
        sa.Column("qr_code_path", sa.Text(), nullable=True),
        sa.Column("total_gb", sa.BigInteger(), nullable=False, server_default="0"),
        sa.Column("used_gb", sa.BigInteger(), nullable=False, server_default="0"),
        sa.Column("ip_limit", sa.Integer(), nullable=True),
        sa.Column("expire_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("status", sa.String(length=16), nullable=False, server_default="provisioning"),
        sa.Column("last_error", sa.Text(), nullable=True),
        sa.Column("last_traffic_sync_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("provisioned_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("disabled_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("deleted_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("created_at", sa.DateTime(timezone=True),
                  server_default=sa.func.now(), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True),
                  server_default=sa.func.now(), nullable=False),
        sa.UniqueConstraint("order_id", name="uq_v2ray_services_order"),
        sa.UniqueConstraint("xui_server_id", "xui_inbound_id", "client_email",
                            name="uq_v2ray_services_server_inbound_email"),
    )
    op.create_index("ix_v2ray_services_user_id", "v2ray_services", ["user_id"])
    op.create_index("ix_v2ray_services_order_id", "v2ray_services", ["order_id"])
    op.create_index("ix_v2ray_services_product_id", "v2ray_services", ["product_id"])
    op.create_index("ix_v2ray_services_xui_server_id", "v2ray_services", ["xui_server_id"])
    op.create_index("ix_v2ray_services_xui_inbound_id", "v2ray_services", ["xui_inbound_id"])
    op.create_index("ix_v2ray_services_client_email", "v2ray_services", ["client_email"])
    op.create_index("ix_v2ray_services_status", "v2ray_services", ["status"])

    # Optional public subscription host/path for building user sub links.
    with op.batch_alter_table("xui_servers") as batch:
        batch.add_column(sa.Column("public_sub_base_url", sa.String(length=255), nullable=True))
        batch.add_column(sa.Column("subscription_path", sa.String(length=120), nullable=True))


def downgrade() -> None:
    with op.batch_alter_table("xui_servers") as batch:
        batch.drop_column("subscription_path")
        batch.drop_column("public_sub_base_url")
    for ix in (
        "ix_v2ray_services_status",
        "ix_v2ray_services_client_email",
        "ix_v2ray_services_xui_inbound_id",
        "ix_v2ray_services_xui_server_id",
        "ix_v2ray_services_product_id",
        "ix_v2ray_services_order_id",
        "ix_v2ray_services_user_id",
    ):
        op.drop_index(ix, table_name="v2ray_services")
    op.drop_table("v2ray_services")

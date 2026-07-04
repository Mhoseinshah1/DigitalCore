"""xui_servers + xui_inbounds tables (3X-UI integration foundation)

Revision ID: 0007_xui_servers
Revises: 0006_products
Create Date: 2025-01-07 00:00:00
"""
from alembic import op
import sqlalchemy as sa

revision = "0007_xui_servers"
down_revision = "0006_products"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.create_table(
        "xui_servers",
        sa.Column("id", sa.Integer(), primary_key=True),
        sa.Column("name", sa.String(length=120), nullable=False),
        sa.Column("base_url", sa.String(length=255), nullable=False),
        sa.Column("web_base_path", sa.String(length=120), nullable=True),
        sa.Column("panel_type", sa.String(length=32), nullable=False, server_default="3x-ui"),
        sa.Column("panel_version", sa.String(length=32), nullable=False, server_default="2.9.4"),
        sa.Column("username", sa.String(length=120), nullable=False),
        sa.Column("encrypted_password", sa.Text(), nullable=False),
        sa.Column("encrypted_api_token", sa.Text(), nullable=True),
        sa.Column("status", sa.String(length=16), nullable=False, server_default="unknown"),
        sa.Column("last_health_check", sa.DateTime(timezone=True), nullable=True),
        sa.Column("created_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False),
    )

    op.create_table(
        "xui_inbounds",
        sa.Column("id", sa.Integer(), primary_key=True),
        sa.Column(
            "server_id",
            sa.Integer(),
            sa.ForeignKey("xui_servers.id", ondelete="CASCADE"),
            nullable=False,
        ),
        sa.Column("inbound_id", sa.Integer(), nullable=False),
        sa.Column("remark", sa.String(length=255), nullable=True),
        sa.Column("protocol", sa.String(length=32), nullable=True),
        sa.Column("port", sa.Integer(), nullable=True),
        sa.Column("is_active", sa.Boolean(), nullable=False, server_default=sa.true()),
        sa.Column("created_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False),
        sa.UniqueConstraint("server_id", "inbound_id", name="uq_xui_inbounds_server_inbound"),
    )
    op.create_index("ix_xui_inbounds_server_id", "xui_inbounds", ["server_id"])


def downgrade() -> None:
    op.drop_index("ix_xui_inbounds_server_id", table_name="xui_inbounds")
    op.drop_table("xui_inbounds")
    op.drop_table("xui_servers")

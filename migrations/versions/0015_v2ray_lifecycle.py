"""phase 8: v2ray lifecycle fields + product/order service actions

Additive: lifecycle stamps on v2ray_services, action_type/applies_to_service on
products, and action_type/target_service_id on orders. Runs on a fresh and an
existing database.

Revision ID: 0015_v2ray_lifecycle
Revises: 0014_wallet_topups
Create Date: 2025-02-10 00:00:00
"""
from alembic import op
import sqlalchemy as sa

revision = "0015_v2ray_lifecycle"
down_revision = "0014_wallet_topups"
branch_labels = None
depends_on = None


def upgrade() -> None:
    with op.batch_alter_table("v2ray_services") as batch:
        batch.add_column(sa.Column("expired_at", sa.DateTime(timezone=True), nullable=True))
        batch.add_column(sa.Column("over_quota_at", sa.DateTime(timezone=True), nullable=True))
        batch.add_column(sa.Column("last_expiry_warning_at", sa.DateTime(timezone=True), nullable=True))
        batch.add_column(sa.Column("last_traffic_warning_at", sa.DateTime(timezone=True), nullable=True))

    with op.batch_alter_table("products") as batch:
        batch.add_column(sa.Column("action_type", sa.String(length=20), nullable=True))
        batch.add_column(sa.Column("applies_to_service", sa.Boolean(), nullable=False,
                                   server_default="0"))

    with op.batch_alter_table("orders") as batch:
        batch.add_column(sa.Column("action_type", sa.String(length=20), nullable=True))
        batch.add_column(sa.Column("target_service_id", sa.Integer(), nullable=True))
        batch.create_foreign_key("fk_orders_target_service", "v2ray_services",
                                 ["target_service_id"], ["id"], ondelete="SET NULL")
    op.create_index("ix_orders_target_service_id", "orders", ["target_service_id"])


def downgrade() -> None:
    op.drop_index("ix_orders_target_service_id", table_name="orders")
    with op.batch_alter_table("orders") as batch:
        batch.drop_constraint("fk_orders_target_service", type_="foreignkey")
        batch.drop_column("target_service_id")
        batch.drop_column("action_type")

    with op.batch_alter_table("products") as batch:
        batch.drop_column("applies_to_service")
        batch.drop_column("action_type")

    with op.batch_alter_table("v2ray_services") as batch:
        batch.drop_column("last_traffic_warning_at")
        batch.drop_column("last_expiry_warning_at")
        batch.drop_column("over_quota_at")
        batch.drop_column("expired_at")

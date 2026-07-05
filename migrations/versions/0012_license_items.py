"""phase 5: license_items stock table + orders.delivery_error; drop legacy
license_keys pool (superseded by license_items)

Additive except for dropping the empty Phase 4 `license_keys` table, which the
real license model replaces. Runs on a fresh and an existing database.

Revision ID: 0012_license_items
Revises: 0011_phase4_actions
Create Date: 2025-01-12 00:00:00
"""
from alembic import op
import sqlalchemy as sa

revision = "0012_license_items"
down_revision = "0011_phase4_actions"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.create_table(
        "license_items",
        sa.Column("id", sa.Integer(), primary_key=True),
        sa.Column("product_id", sa.Integer(),
                  sa.ForeignKey("products.id", ondelete="CASCADE"), nullable=False),
        sa.Column("email", sa.String(length=255), nullable=False),
        sa.Column("password", sa.String(length=255), nullable=False),
        sa.Column("note", sa.Text(), nullable=True),
        sa.Column("status", sa.String(length=16), nullable=False, server_default="available"),
        sa.Column("sold_to_user_id", sa.Integer(),
                  sa.ForeignKey("users.id", ondelete="SET NULL"), nullable=True),
        sa.Column("order_id", sa.Integer(),
                  sa.ForeignKey("orders.id", ondelete="SET NULL"), nullable=True),
        sa.Column("reserved_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("sold_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("imported_by_admin_id", sa.Integer(),
                  sa.ForeignKey("admins.id", ondelete="SET NULL"), nullable=True),
        sa.Column("replaced_by_license_id", sa.Integer(),
                  sa.ForeignKey("license_items.id", ondelete="SET NULL"), nullable=True),
        sa.Column("admin_note", sa.Text(), nullable=True),
        sa.Column("created_at", sa.DateTime(timezone=True),
                  server_default=sa.func.now(), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True),
                  server_default=sa.func.now(), nullable=False),
        sa.UniqueConstraint("product_id", "email", name="uq_license_items_product_email"),
    )
    op.create_index("ix_license_items_product_id", "license_items", ["product_id"])
    op.create_index("ix_license_items_status", "license_items", ["status"])
    op.create_index("ix_license_items_sold_to_user_id", "license_items", ["sold_to_user_id"])
    op.create_index("ix_license_items_order_id", "license_items", ["order_id"])

    with op.batch_alter_table("orders") as batch:
        batch.add_column(sa.Column("delivery_error", sa.Text(), nullable=True))

    # Drop the Phase 4 code-pool table (superseded, always empty at this point).
    op.drop_index("ix_license_keys_is_used", table_name="license_keys")
    op.drop_index("ix_license_keys_product_id", table_name="license_keys")
    op.drop_table("license_keys")


def downgrade() -> None:
    # Recreate the legacy license_keys table.
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

    with op.batch_alter_table("orders") as batch:
        batch.drop_column("delivery_error")

    op.drop_index("ix_license_items_order_id", table_name="license_items")
    op.drop_index("ix_license_items_sold_to_user_id", table_name="license_items")
    op.drop_index("ix_license_items_status", table_name="license_items")
    op.drop_index("ix_license_items_product_id", table_name="license_items")
    op.drop_table("license_items")

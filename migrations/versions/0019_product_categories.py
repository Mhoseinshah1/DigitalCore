"""bot UX: product categories + products.category_id

Additive:
  - new table product_categories (id, title, slug unique, description, sort_order,
    is_active, created_at, updated_at) — an admin-managed grouping so the bot can
    show categories first, then products in a category.
  - products: add category_id (nullable FK product_categories, ON DELETE SET NULL).
    Existing products stay uncategorised (NULL); the bot groups those under a
    synthetic "سایر محصولات" (Other) so no data backfill is needed.

Runs on a fresh database and an existing one. batch_alter_table keeps the
products column add SQLite-safe.

Revision ID: 0019_product_categories
Revises: 0018_backup_jobs
Create Date: 2025-03-31 00:00:00
"""
from alembic import op
import sqlalchemy as sa

revision = "0019_product_categories"
down_revision = "0018_backup_jobs"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.create_table(
        "product_categories",
        sa.Column("id", sa.Integer(), primary_key=True),
        sa.Column("title", sa.String(length=200), nullable=False),
        sa.Column("slug", sa.String(length=200), nullable=False),
        sa.Column("description", sa.Text(), nullable=True),
        sa.Column("sort_order", sa.Integer(), nullable=False, server_default="0"),
        sa.Column("is_active", sa.Boolean(), nullable=False, server_default="1"),
        sa.Column("created_at", sa.DateTime(timezone=True),
                  server_default=sa.func.now(), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True),
                  server_default=sa.func.now(), nullable=False),
    )
    op.create_index(
        "ix_product_categories_slug", "product_categories", ["slug"], unique=True
    )
    op.create_index(
        "ix_product_categories_is_active", "product_categories", ["is_active"]
    )

    with op.batch_alter_table("products") as batch:
        batch.add_column(sa.Column("category_id", sa.Integer(), nullable=True))
        batch.create_foreign_key(
            "fk_products_category_id", "product_categories", ["category_id"], ["id"],
            ondelete="SET NULL",
        )
    op.create_index("ix_products_category_id", "products", ["category_id"])


def downgrade() -> None:
    op.drop_index("ix_products_category_id", table_name="products")
    with op.batch_alter_table("products") as batch:
        batch.drop_constraint("fk_products_category_id", type_="foreignkey")
        batch.drop_column("category_id")

    op.drop_index("ix_product_categories_is_active", table_name="product_categories")
    op.drop_index("ix_product_categories_slug", table_name="product_categories")
    op.drop_table("product_categories")

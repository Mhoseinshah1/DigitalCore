"""phase 10: coupons + referrals

Additive: three new tables (coupons, coupon_usages, referral_rewards), two new
columns on users (referral_code, referral_registered_at), and two on orders
(coupon_id, coupon_code). users.referrer_id already exists (Phase 1). Runs on a
fresh and an existing database.

Revision ID: 0017_coupons_referrals
Revises: 0016_tickets_tutorials
Create Date: 2025-03-10 00:00:00
"""
from alembic import op
import sqlalchemy as sa

revision = "0017_coupons_referrals"
down_revision = "0016_tickets_tutorials"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.create_table(
        "coupons",
        sa.Column("id", sa.Integer(), primary_key=True),
        sa.Column("code", sa.String(length=64), nullable=False),
        sa.Column("title", sa.String(length=200), nullable=True),
        sa.Column("description", sa.Text(), nullable=True),
        sa.Column("discount_type", sa.String(length=10), nullable=False, server_default="percent"),
        sa.Column("discount_value", sa.BigInteger(), nullable=False, server_default="0"),
        sa.Column("max_discount_amount", sa.BigInteger(), nullable=True),
        sa.Column("min_order_amount", sa.BigInteger(), nullable=True),
        sa.Column("usage_limit", sa.Integer(), nullable=True),
        sa.Column("usage_limit_per_user", sa.Integer(), nullable=True),
        sa.Column("used_count", sa.Integer(), nullable=False, server_default="0"),
        sa.Column("starts_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("expires_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("is_active", sa.Boolean(), nullable=False, server_default=sa.true()),
        sa.Column("product_id", sa.Integer(),
                  sa.ForeignKey("products.id", ondelete="SET NULL"), nullable=True),
        sa.Column("product_type", sa.String(length=16), nullable=True),
        sa.Column("applies_to_action", sa.String(length=20), nullable=True),
        sa.Column("created_by_admin_id", sa.Integer(),
                  sa.ForeignKey("admins.id", ondelete="SET NULL"), nullable=True),
        sa.Column("created_at", sa.DateTime(timezone=True),
                  server_default=sa.func.now(), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True),
                  server_default=sa.func.now(), nullable=False),
    )
    op.create_index("ix_coupons_code", "coupons", ["code"], unique=True)
    op.create_index("ix_coupons_is_active", "coupons", ["is_active"])
    op.create_index("ix_coupons_product_id", "coupons", ["product_id"])

    op.create_table(
        "coupon_usages",
        sa.Column("id", sa.Integer(), primary_key=True),
        sa.Column("coupon_id", sa.Integer(),
                  sa.ForeignKey("coupons.id", ondelete="CASCADE"), nullable=False),
        sa.Column("user_id", sa.Integer(),
                  sa.ForeignKey("users.id", ondelete="CASCADE"), nullable=False),
        sa.Column("order_id", sa.Integer(),
                  sa.ForeignKey("orders.id", ondelete="CASCADE"), nullable=False),
        sa.Column("discount_amount", sa.BigInteger(), nullable=False, server_default="0"),
        sa.Column("created_at", sa.DateTime(timezone=True),
                  server_default=sa.func.now(), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True),
                  server_default=sa.func.now(), nullable=False),
        sa.UniqueConstraint("coupon_id", "order_id", name="uq_coupon_usages_coupon_order"),
    )
    op.create_index("ix_coupon_usages_coupon_id", "coupon_usages", ["coupon_id"])
    op.create_index("ix_coupon_usages_user_id", "coupon_usages", ["user_id"])
    op.create_index("ix_coupon_usages_order_id", "coupon_usages", ["order_id"])

    op.create_table(
        "referral_rewards",
        sa.Column("id", sa.Integer(), primary_key=True),
        sa.Column("referrer_user_id", sa.Integer(),
                  sa.ForeignKey("users.id", ondelete="CASCADE"), nullable=False),
        sa.Column("referred_user_id", sa.Integer(),
                  sa.ForeignKey("users.id", ondelete="CASCADE"), nullable=False),
        sa.Column("order_id", sa.Integer(),
                  sa.ForeignKey("orders.id", ondelete="SET NULL"), nullable=True),
        sa.Column("reward_type", sa.String(length=10), nullable=False, server_default="fixed"),
        sa.Column("reward_amount", sa.BigInteger(), nullable=False, server_default="0"),
        sa.Column("status", sa.String(length=12), nullable=False, server_default="pending"),
        sa.Column("reject_reason", sa.Text(), nullable=True),
        sa.Column("approved_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("paid_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("cancelled_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("created_at", sa.DateTime(timezone=True),
                  server_default=sa.func.now(), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True),
                  server_default=sa.func.now(), nullable=False),
        sa.UniqueConstraint("order_id", name="uq_referral_rewards_order"),
    )
    op.create_index("ix_referral_rewards_referrer_user_id", "referral_rewards",
                    ["referrer_user_id"])
    op.create_index("ix_referral_rewards_referred_user_id", "referral_rewards",
                    ["referred_user_id"])
    op.create_index("ix_referral_rewards_order_id", "referral_rewards", ["order_id"])
    op.create_index("ix_referral_rewards_status", "referral_rewards", ["status"])

    with op.batch_alter_table("users") as batch:
        batch.add_column(sa.Column("referral_code", sa.String(length=32), nullable=True))
        batch.add_column(sa.Column("referral_registered_at", sa.DateTime(timezone=True),
                                   nullable=True))
    op.create_index("ix_users_referral_code", "users", ["referral_code"], unique=True)

    with op.batch_alter_table("orders") as batch:
        batch.add_column(sa.Column("coupon_id", sa.Integer(), nullable=True))
        batch.add_column(sa.Column("coupon_code", sa.String(length=64), nullable=True))
        batch.create_foreign_key("fk_orders_coupon_id", "coupons",
                                 ["coupon_id"], ["id"], ondelete="SET NULL")
    op.create_index("ix_orders_coupon_id", "orders", ["coupon_id"])


def downgrade() -> None:
    op.drop_index("ix_orders_coupon_id", table_name="orders")
    with op.batch_alter_table("orders") as batch:
        batch.drop_constraint("fk_orders_coupon_id", type_="foreignkey")
        batch.drop_column("coupon_code")
        batch.drop_column("coupon_id")

    op.drop_index("ix_users_referral_code", table_name="users")
    with op.batch_alter_table("users") as batch:
        batch.drop_column("referral_registered_at")
        batch.drop_column("referral_code")

    op.drop_index("ix_referral_rewards_status", table_name="referral_rewards")
    op.drop_index("ix_referral_rewards_order_id", table_name="referral_rewards")
    op.drop_index("ix_referral_rewards_referred_user_id", table_name="referral_rewards")
    op.drop_index("ix_referral_rewards_referrer_user_id", table_name="referral_rewards")
    op.drop_table("referral_rewards")

    op.drop_index("ix_coupon_usages_order_id", table_name="coupon_usages")
    op.drop_index("ix_coupon_usages_user_id", table_name="coupon_usages")
    op.drop_index("ix_coupon_usages_coupon_id", table_name="coupon_usages")
    op.drop_table("coupon_usages")

    op.drop_index("ix_coupons_product_id", table_name="coupons")
    op.drop_index("ix_coupons_is_active", table_name="coupons")
    op.drop_index("ix_coupons_code", table_name="coupons")
    op.drop_table("coupons")

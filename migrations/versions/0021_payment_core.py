"""Payment Core slice 1: invoices, payment methods, payment extensions.

Additive + one relaxation (payments.order_id becomes nullable so wallet top-up
payments fit the same table). Existing rows keep working: new payment columns
default to safe values and `approved` remains the "paid" status.

Seeds the six default payment methods (only wallet + manual_receipt active).

Revision ID: 0021_payment_core
Revises: 0020_xui_auth_and_inbound_sync
Create Date: 2025-04-08 00:00:00
"""
from alembic import op
import sqlalchemy as sa

revision = "0021_payment_core"
down_revision = "0020_xui_auth_and_inbound_sync"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.create_table(
        "invoices",
        sa.Column("id", sa.Integer(), primary_key=True),
        sa.Column("invoice_number", sa.String(length=40), nullable=False),
        sa.Column("tracking_code", sa.String(length=40), nullable=False),
        sa.Column("user_id", sa.Integer(),
                  sa.ForeignKey("users.id", ondelete="CASCADE"), nullable=False),
        sa.Column("order_id", sa.Integer(),
                  sa.ForeignKey("orders.id", ondelete="SET NULL"), nullable=True),
        sa.Column("product_id", sa.Integer(),
                  sa.ForeignKey("products.id", ondelete="SET NULL"), nullable=True),
        sa.Column("invoice_type", sa.String(length=20), nullable=False,
                  server_default="product_purchase"),
        sa.Column("amount", sa.BigInteger(), nullable=False, server_default="0"),
        sa.Column("discount_amount", sa.BigInteger(), nullable=False, server_default="0"),
        sa.Column("final_amount", sa.BigInteger(), nullable=False, server_default="0"),
        sa.Column("currency", sa.String(length=10), nullable=False, server_default="toman"),
        sa.Column("status", sa.String(length=16), nullable=False, server_default="unpaid"),
        sa.Column("metadata_json", sa.Text(), nullable=True),
        sa.Column("expires_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("paid_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("cancelled_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("created_at", sa.DateTime(timezone=True),
                  server_default=sa.func.now(), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True),
                  server_default=sa.func.now(), nullable=False),
    )
    op.create_index("ix_invoices_user_id", "invoices", ["user_id"])
    op.create_index("ix_invoices_order_id", "invoices", ["order_id"])
    op.create_index("ix_invoices_product_id", "invoices", ["product_id"])
    op.create_index("ix_invoices_invoice_type", "invoices", ["invoice_type"])
    op.create_index("ix_invoices_status", "invoices", ["status"])
    op.create_index("ix_invoices_invoice_number", "invoices", ["invoice_number"], unique=True)
    op.create_index("ix_invoices_tracking_code", "invoices", ["tracking_code"], unique=True)

    op.create_table(
        "payment_methods",
        sa.Column("id", sa.Integer(), primary_key=True),
        sa.Column("code", sa.String(length=40), nullable=False),
        sa.Column("title", sa.String(length=120), nullable=False),
        sa.Column("method_type", sa.String(length=20), nullable=False),
        sa.Column("is_active", sa.Boolean(), nullable=False, server_default="1"),
        sa.Column("sort_order", sa.Integer(), nullable=False, server_default="0"),
        sa.Column("min_amount", sa.BigInteger(), nullable=True),
        sa.Column("max_amount", sa.BigInteger(), nullable=True),
        sa.Column("cashback_percent", sa.Numeric(5, 2), nullable=False, server_default="0"),
        sa.Column("activate_after_payments", sa.Integer(), nullable=False, server_default="0"),
        sa.Column("api_url", sa.String(length=255), nullable=True),
        sa.Column("api_token_encrypted", sa.Text(), nullable=True),
        sa.Column("merchant_id_encrypted", sa.Text(), nullable=True),
        sa.Column("instruction_text", sa.Text(), nullable=True),
        sa.Column("created_at", sa.DateTime(timezone=True),
                  server_default=sa.func.now(), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True),
                  server_default=sa.func.now(), nullable=False),
    )
    op.create_index("ix_payment_methods_code", "payment_methods", ["code"], unique=True)

    with op.batch_alter_table("payments") as batch:
        batch.alter_column("order_id", existing_type=sa.Integer(), nullable=True)
        batch.add_column(sa.Column("invoice_id", sa.Integer(), nullable=True))
        batch.create_foreign_key(
            "fk_payments_invoice_id", "invoices", ["invoice_id"], ["id"],
            ondelete="SET NULL",
        )
        batch.add_column(sa.Column(
            "payment_type", sa.String(length=20), nullable=False,
            server_default="product_purchase"))
        batch.add_column(sa.Column(
            "bonus_amount", sa.BigInteger(), nullable=False, server_default="0"))
        batch.add_column(sa.Column("final_wallet_credit", sa.BigInteger(), nullable=True))
        batch.add_column(sa.Column("provider_name", sa.String(length=60), nullable=True))
        batch.add_column(sa.Column("provider_payment_id", sa.String(length=120), nullable=True))
        batch.add_column(sa.Column("provider_url", sa.String(length=512), nullable=True))
        batch.add_column(sa.Column("reject_reason", sa.Text(), nullable=True))
        batch.add_column(sa.Column("metadata_json", sa.Text(), nullable=True))
        batch.add_column(sa.Column("expired_at", sa.DateTime(timezone=True), nullable=True))
    # Unique tracking codes (multiple NULLs are allowed on every backend we target,
    # so legacy rows without a code are unaffected).
    op.create_index("ix_payments_tracking_code", "payments", ["tracking_code"], unique=True)
    op.create_index("ix_payments_invoice_id", "payments", ["invoice_id"])

    # Seed the default methods (idempotent: skip codes that already exist).
    conn = op.get_bind()
    existing = {r[0] for r in conn.execute(sa.text("SELECT code FROM payment_methods"))}
    seeds = [
        ("wallet", "کیف پول", "wallet", True, 1),
        ("manual_receipt", "کارت به کارت", "manual_receipt", True, 2),
        ("custom_gateway", "درگاه سفارشی", "custom_gateway", False, 3),
        ("online_gateway", "درگاه پرداخت آنلاین", "online_gateway", False, 4),
        ("crypto", "ارز دیجیتال", "crypto", False, 5),
        ("telegram_stars", "استار تلگرام", "telegram_stars", False, 6),
    ]
    for code, title, mtype, active, order in seeds:
        if code in existing:
            continue
        conn.execute(
            sa.text(
                "INSERT INTO payment_methods "
                "(code, title, method_type, is_active, sort_order, cashback_percent, "
                " activate_after_payments, created_at, updated_at) "
                "VALUES (:code, :title, :mtype, :active, :ord, 0, 0, "
                "CURRENT_TIMESTAMP, CURRENT_TIMESTAMP)"
            ),
            {"code": code, "title": title, "mtype": mtype, "active": active, "ord": order},
        )


def downgrade() -> None:
    op.drop_index("ix_payments_invoice_id", table_name="payments")
    op.drop_index("ix_payments_tracking_code", table_name="payments")
    with op.batch_alter_table("payments") as batch:
        batch.drop_column("expired_at")
        batch.drop_column("metadata_json")
        batch.drop_column("reject_reason")
        batch.drop_column("provider_url")
        batch.drop_column("provider_payment_id")
        batch.drop_column("provider_name")
        batch.drop_column("final_wallet_credit")
        batch.drop_column("bonus_amount")
        batch.drop_column("payment_type")
        batch.drop_column("invoice_id")
        batch.alter_column("order_id", existing_type=sa.Integer(), nullable=False)

    op.drop_index("ix_payment_methods_code", table_name="payment_methods")
    op.drop_table("payment_methods")
    for name in ("ix_invoices_tracking_code", "ix_invoices_invoice_number",
                 "ix_invoices_status", "ix_invoices_invoice_type",
                 "ix_invoices_product_id", "ix_invoices_order_id", "ix_invoices_user_id"):
        op.drop_index(name, table_name="invoices")
    op.drop_table("invoices")

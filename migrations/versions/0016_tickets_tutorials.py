"""phase 9: support tickets + tutorials / knowledge base

Additive: four new tables (tickets, ticket_messages, tutorial_categories,
tutorials). No changes to existing tables. Runs on a fresh and an existing
database.

Revision ID: 0016_tickets_tutorials
Revises: 0015_v2ray_lifecycle
Create Date: 2025-02-24 00:00:00
"""
from alembic import op
import sqlalchemy as sa

revision = "0016_tickets_tutorials"
down_revision = "0015_v2ray_lifecycle"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.create_table(
        "tickets",
        sa.Column("id", sa.Integer(), primary_key=True),
        sa.Column("ticket_number", sa.String(length=40), nullable=False),
        sa.Column("user_id", sa.Integer(),
                  sa.ForeignKey("users.id", ondelete="CASCADE"), nullable=False),
        sa.Column("subject", sa.String(length=200), nullable=False),
        sa.Column("status", sa.String(length=20), nullable=False, server_default="open"),
        sa.Column("priority", sa.String(length=10), nullable=False, server_default="normal"),
        sa.Column("assigned_admin_id", sa.Integer(),
                  sa.ForeignKey("admins.id", ondelete="SET NULL"), nullable=True),
        sa.Column("closed_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("last_message_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("created_at", sa.DateTime(timezone=True),
                  server_default=sa.func.now(), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True),
                  server_default=sa.func.now(), nullable=False),
    )
    op.create_index("ix_tickets_ticket_number", "tickets", ["ticket_number"], unique=True)
    op.create_index("ix_tickets_user_id", "tickets", ["user_id"])
    op.create_index("ix_tickets_status", "tickets", ["status"])
    op.create_index("ix_tickets_priority", "tickets", ["priority"])
    op.create_index("ix_tickets_assigned_admin_id", "tickets", ["assigned_admin_id"])

    op.create_table(
        "ticket_messages",
        sa.Column("id", sa.Integer(), primary_key=True),
        sa.Column("ticket_id", sa.Integer(),
                  sa.ForeignKey("tickets.id", ondelete="CASCADE"), nullable=False),
        sa.Column("sender_type", sa.String(length=10), nullable=False),
        sa.Column("sender_user_id", sa.Integer(),
                  sa.ForeignKey("users.id", ondelete="SET NULL"), nullable=True),
        sa.Column("sender_admin_id", sa.Integer(),
                  sa.ForeignKey("admins.id", ondelete="SET NULL"), nullable=True),
        sa.Column("message", sa.Text(), nullable=False, server_default=""),
        sa.Column("attachment_path", sa.String(length=512), nullable=True),
        sa.Column("attachment_file_id", sa.String(length=255), nullable=True),
        sa.Column("attachment_mime_type", sa.String(length=128), nullable=True),
        sa.Column("attachment_original_name", sa.String(length=255), nullable=True),
        sa.Column("attachment_size", sa.Integer(), nullable=True),
        sa.Column("created_at", sa.DateTime(timezone=True),
                  server_default=sa.func.now(), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True),
                  server_default=sa.func.now(), nullable=False),
    )
    op.create_index("ix_ticket_messages_ticket_id", "ticket_messages", ["ticket_id"])

    op.create_table(
        "tutorial_categories",
        sa.Column("id", sa.Integer(), primary_key=True),
        sa.Column("title", sa.String(length=200), nullable=False),
        sa.Column("slug", sa.String(length=200), nullable=False),
        sa.Column("sort_order", sa.Integer(), nullable=False, server_default="0"),
        sa.Column("is_active", sa.Boolean(), nullable=False, server_default=sa.true()),
        sa.Column("created_at", sa.DateTime(timezone=True),
                  server_default=sa.func.now(), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True),
                  server_default=sa.func.now(), nullable=False),
    )
    op.create_index("ix_tutorial_categories_slug", "tutorial_categories", ["slug"], unique=True)

    op.create_table(
        "tutorials",
        sa.Column("id", sa.Integer(), primary_key=True),
        sa.Column("category_id", sa.Integer(),
                  sa.ForeignKey("tutorial_categories.id", ondelete="SET NULL"), nullable=True),
        sa.Column("title", sa.String(length=200), nullable=False),
        sa.Column("slug", sa.String(length=200), nullable=False),
        sa.Column("content", sa.Text(), nullable=False, server_default=""),
        sa.Column("platform", sa.String(length=20), nullable=True),
        sa.Column("product_type", sa.String(length=20), nullable=True),
        sa.Column("sort_order", sa.Integer(), nullable=False, server_default="0"),
        sa.Column("is_active", sa.Boolean(), nullable=False, server_default=sa.true()),
        sa.Column("created_at", sa.DateTime(timezone=True),
                  server_default=sa.func.now(), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True),
                  server_default=sa.func.now(), nullable=False),
    )
    op.create_index("ix_tutorials_slug", "tutorials", ["slug"], unique=True)
    op.create_index("ix_tutorials_category_id", "tutorials", ["category_id"])
    op.create_index("ix_tutorials_platform", "tutorials", ["platform"])
    op.create_index("ix_tutorials_product_type", "tutorials", ["product_type"])
    op.create_index("ix_tutorials_is_active", "tutorials", ["is_active"])


def downgrade() -> None:
    op.drop_index("ix_tutorials_is_active", table_name="tutorials")
    op.drop_index("ix_tutorials_product_type", table_name="tutorials")
    op.drop_index("ix_tutorials_platform", table_name="tutorials")
    op.drop_index("ix_tutorials_category_id", table_name="tutorials")
    op.drop_index("ix_tutorials_slug", table_name="tutorials")
    op.drop_table("tutorials")

    op.drop_index("ix_tutorial_categories_slug", table_name="tutorial_categories")
    op.drop_table("tutorial_categories")

    op.drop_index("ix_ticket_messages_ticket_id", table_name="ticket_messages")
    op.drop_table("ticket_messages")

    op.drop_index("ix_tickets_assigned_admin_id", table_name="tickets")
    op.drop_index("ix_tickets_priority", table_name="tickets")
    op.drop_index("ix_tickets_status", table_name="tickets")
    op.drop_index("ix_tickets_user_id", table_name="tickets")
    op.drop_index("ix_tickets_ticket_number", table_name="tickets")
    op.drop_table("tickets")

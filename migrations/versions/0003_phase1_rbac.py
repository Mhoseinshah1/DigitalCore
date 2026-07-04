"""phase 1: user profile columns, admin role, audit_logs table

Revision ID: 0003_phase1_rbac
Revises: 0002_admin_username
Create Date: 2025-01-03 00:00:00
"""
from alembic import op
import sqlalchemy as sa

revision = "0003_phase1_rbac"
down_revision = "0002_admin_username"
branch_labels = None
depends_on = None


def upgrade() -> None:
    # --- users: profile/activity columns + self-referential referrer FK ------
    with op.batch_alter_table("users") as batch:
        batch.add_column(sa.Column("phone_number", sa.String(length=32), nullable=True))
        batch.add_column(
            sa.Column("is_blocked", sa.Boolean(), nullable=False, server_default=sa.false())
        )
        batch.add_column(
            sa.Column("is_verified", sa.Boolean(), nullable=False, server_default=sa.false())
        )
        batch.add_column(sa.Column("referrer_id", sa.Integer(), nullable=True))
        batch.add_column(sa.Column("last_activity_at", sa.DateTime(timezone=True), nullable=True))
        batch.create_foreign_key(
            "fk_users_referrer_id_users", "users", ["referrer_id"], ["id"]
        )

    # --- admins: RBAC role ----------------------------------------------------
    with op.batch_alter_table("admins") as batch:
        batch.add_column(
            sa.Column("role", sa.String(length=32), nullable=False, server_default="admin")
        )
    # Existing super admins become owners.
    op.execute("UPDATE admins SET role = 'owner' WHERE is_super_admin")

    # --- audit_logs -------------------------------------------------------------
    op.create_table(
        "audit_logs",
        sa.Column("id", sa.Integer(), primary_key=True),
        sa.Column("actor_type", sa.String(length=16), nullable=False),
        sa.Column("actor_id", sa.BigInteger(), nullable=True),
        sa.Column("action", sa.String(length=128), nullable=False),
        sa.Column("target_type", sa.String(length=64), nullable=True),
        sa.Column("target_id", sa.String(length=64), nullable=True),
        sa.Column("old_value", sa.Text(), nullable=True),
        sa.Column("new_value", sa.Text(), nullable=True),
        sa.Column(
            "created_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False
        ),
    )
    op.create_index("ix_audit_logs_action", "audit_logs", ["action"])
    op.create_index("ix_audit_logs_created_at", "audit_logs", ["created_at"])


def downgrade() -> None:
    op.drop_index("ix_audit_logs_created_at", table_name="audit_logs")
    op.drop_index("ix_audit_logs_action", table_name="audit_logs")
    op.drop_table("audit_logs")

    with op.batch_alter_table("admins") as batch:
        batch.drop_column("role")

    with op.batch_alter_table("users") as batch:
        batch.drop_constraint("fk_users_referrer_id_users", type_="foreignkey")
        batch.drop_column("last_activity_at")
        batch.drop_column("referrer_id")
        batch.drop_column("is_verified")
        batch.drop_column("is_blocked")
        batch.drop_column("phone_number")

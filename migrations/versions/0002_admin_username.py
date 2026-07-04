"""admins: add username (unique, nullable); make email nullable

Revision ID: 0002_admin_username
Revises: 0001_initial
Create Date: 2025-01-02 00:00:00
"""
from alembic import op
import sqlalchemy as sa

revision = "0002_admin_username"
down_revision = "0001_initial"
branch_labels = None
depends_on = None


def upgrade() -> None:
    # batch_alter_table emits plain ALTERs on PostgreSQL and handles SQLite's
    # table-rebuild requirement for the nullability change in dev/test runs.
    with op.batch_alter_table("admins") as batch:
        batch.add_column(sa.Column("username", sa.String(length=150), nullable=True))
        batch.alter_column(
            "email", existing_type=sa.String(length=255), nullable=True
        )
    op.create_index("ix_admins_username", "admins", ["username"], unique=True)


def downgrade() -> None:
    op.drop_index("ix_admins_username", table_name="admins")
    with op.batch_alter_table("admins") as batch:
        batch.alter_column(
            "email", existing_type=sa.String(length=255), nullable=False
        )
        batch.drop_column("username")

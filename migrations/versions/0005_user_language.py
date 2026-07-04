"""users: add language column (fa default; existing users backfilled to fa)

Revision ID: 0005_user_language
Revises: 0004_settings_keys
Create Date: 2025-01-05 00:00:00
"""
from alembic import op
import sqlalchemy as sa

revision = "0005_user_language"
down_revision = "0004_settings_keys"
branch_labels = None
depends_on = None


def upgrade() -> None:
    with op.batch_alter_table("users") as batch:
        batch.add_column(
            sa.Column("language", sa.String(length=5), nullable=False, server_default="fa")
        )


def downgrade() -> None:
    with op.batch_alter_table("users") as batch:
        batch.drop_column("language")

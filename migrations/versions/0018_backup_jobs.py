"""phase 12: backup jobs

Additive: one new table (backup_jobs) holding backup *metadata* only — the
backup contents live on disk under storage/backups/, never in the database.
Runs on a fresh and an existing database.

Revision ID: 0018_backup_jobs
Revises: 0017_coupons_referrals
Create Date: 2025-03-24 00:00:00
"""
from alembic import op
import sqlalchemy as sa

revision = "0018_backup_jobs"
down_revision = "0017_coupons_referrals"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.create_table(
        "backup_jobs",
        sa.Column("id", sa.Integer(), primary_key=True),
        sa.Column("backup_type", sa.String(length=16), nullable=False),
        sa.Column("status", sa.String(length=16), nullable=False, server_default="pending"),
        sa.Column("file_path", sa.String(length=512), nullable=True),
        sa.Column("file_name", sa.String(length=255), nullable=True),
        sa.Column("file_size", sa.BigInteger(), nullable=True),
        sa.Column("checksum_sha256", sa.String(length=64), nullable=True),
        sa.Column("created_by_admin_id", sa.Integer(),
                  sa.ForeignKey("admins.id", ondelete="SET NULL"), nullable=True),
        sa.Column("started_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("completed_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("failed_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("error_message", sa.Text(), nullable=True),
        sa.Column("metadata_json", sa.Text(), nullable=True),
        sa.Column("created_at", sa.DateTime(timezone=True),
                  server_default=sa.func.now(), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True),
                  server_default=sa.func.now(), nullable=False),
    )
    op.create_index("ix_backup_jobs_backup_type", "backup_jobs", ["backup_type"])
    op.create_index("ix_backup_jobs_status", "backup_jobs", ["status"])


def downgrade() -> None:
    op.drop_index("ix_backup_jobs_status", table_name="backup_jobs")
    op.drop_index("ix_backup_jobs_backup_type", table_name="backup_jobs")
    op.drop_table("backup_jobs")

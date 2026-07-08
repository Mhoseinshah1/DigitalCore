"""Backup jobs: metadata for a database / storage / full backup (Phase 12).

A row records *only* metadata about a backup — its type, status, on-disk
location (a repo-relative path under ``storage/backups/``), size and SHA-256
checksum. The backup **contents are never stored in the database**, and
``error_message`` is scrubbed of anything secret (DB URL / password) before it
is written (see ``backup_service``).
"""
from __future__ import annotations

from datetime import datetime

from sqlalchemy import BigInteger, DateTime, ForeignKey, String, Text
from sqlalchemy.orm import Mapped, mapped_column

from app.models.base import Base, TimestampMixin

BACKUP_TYPES: tuple[str, ...] = ("database", "storage", "full")
BACKUP_STATUSES: tuple[str, ...] = (
    "pending", "running", "completed", "failed", "deleted",
)


class BackupJob(Base, TimestampMixin):
    __tablename__ = "backup_jobs"

    id: Mapped[int] = mapped_column(primary_key=True)

    backup_type: Mapped[str] = mapped_column(String(16), nullable=False, index=True)
    status: Mapped[str] = mapped_column(
        String(16), nullable=False, default="pending", server_default="pending",
        index=True,
    )

    # Repo-relative path under storage/backups/ (never an absolute or arbitrary
    # path) + the bare file name for display / download.
    file_path: Mapped[str | None] = mapped_column(String(512), nullable=True)
    file_name: Mapped[str | None] = mapped_column(String(255), nullable=True)
    file_size: Mapped[int | None] = mapped_column(BigInteger, nullable=True)
    checksum_sha256: Mapped[str | None] = mapped_column(String(64), nullable=True)

    created_by_admin_id: Mapped[int | None] = mapped_column(
        ForeignKey("admins.id", ondelete="SET NULL"), nullable=True
    )

    started_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    completed_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    failed_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)

    # Scrubbed of secrets before storage.
    error_message: Mapped[str | None] = mapped_column(Text, nullable=True)
    # Free-form JSON context (counts, included dirs, tool used) — no secrets.
    metadata_json: Mapped[str | None] = mapped_column(Text, nullable=True)

    def __repr__(self) -> str:  # pragma: no cover - debug helper
        return (f"<BackupJob id={self.id} type={self.backup_type} "
                f"status={self.status}>")

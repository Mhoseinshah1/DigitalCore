"""Backups (Phase 12): database / storage / full archives + retention.

Design & safety:
  * Backups are written to ``storage/backups/YYYY/MM/`` with mode ``0600``. The
    **contents never touch the database** — only metadata (path, size, SHA-256,
    status) is recorded in ``backup_jobs``.
  * Database dumps use ``pg_dump`` in production (Postgres). The password is
    passed via the ``PGPASSWORD`` environment variable — never on the command
    line or in a log. When ``pg_dump`` is unavailable or the bind is SQLite
    (tests/dev), a **SQLAlchemy logical fallback** dumps table rows as INSERTs;
    production must use ``pg_dump`` (documented).
  * Every ``error_message`` is scrubbed of the DB password and any URL
    credentials before it is stored or logged.
  * Storage backups include receipts/tickets/exports/qrcodes/uploads and
    **exclude** ``storage/backups`` itself.
  * File paths are always validated to live under ``storage/backups`` — no
    traversal, no arbitrary-file access.
"""
from __future__ import annotations

import asyncio
import gzip
import hashlib
import json
import logging
import os
import re
import shutil
import tarfile
import tempfile
from datetime import datetime, timezone
from pathlib import Path
from urllib.parse import urlparse

from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.config import settings
from app.core.settings_service import SettingsService
from app.models.backup_job import BACKUP_TYPES, BackupJob

log = logging.getLogger("backup")

REPO_ROOT = Path(__file__).resolve().parents[2]
STORAGE_ROOT = REPO_ROOT / "storage"
BACKUPS_ROOT = STORAGE_ROOT / "backups"

# storage subdirs bundled into storage / full backups (backups excluded).
STORAGE_SUBDIRS: tuple[str, ...] = ("receipts", "tickets", "exports", "qrcodes", "uploads")

_CHUNK = 1024 * 1024


class BackupError(Exception):
    """Raised for backup problems; messages are already scrubbed of secrets."""


# ==========================================================================
# Helpers
# ==========================================================================
def _now() -> datetime:
    return datetime.now(timezone.utc)


def _aware(dt: datetime) -> datetime:
    return dt if dt.tzinfo else dt.replace(tzinfo=timezone.utc)


def _timestamp() -> str:
    return _now().strftime("%Y%m%d-%H%M%S")


def _scrub(text: str | None) -> str:
    """Strip the DB password and URL credentials from a message."""
    if not text:
        return ""
    out = str(text)
    pw = settings.POSTGRES_PASSWORD
    if pw:
        out = out.replace(pw, "***")
    # redact user:pass@ in any URL
    out = re.sub(r"(://[^:/@\s]+):[^@/\s]+@", r"\1:***@", out)
    return out


def _dated_dir() -> Path:
    d = BACKUPS_ROOT / _now().strftime("%Y") / _now().strftime("%m")
    d.mkdir(parents=True, exist_ok=True)
    return d


def _within(path: Path, root: Path) -> bool:
    try:
        path.resolve().relative_to(root.resolve())
        return True
    except ValueError:
        return False


def _rel(path: Path) -> str:
    return str(path.resolve().relative_to(REPO_ROOT))


def _abs_path(job: BackupJob) -> Path | None:
    """Resolve a job's on-disk path, guaranteeing it lives under storage/backups."""
    if not job.file_path:
        return None
    p = (REPO_ROOT / job.file_path).resolve()
    return p if _within(p, BACKUPS_ROOT) else None


def calculate_sha256(path: str | Path) -> str:
    h = hashlib.sha256()
    with open(path, "rb") as f:
        for chunk in iter(lambda: f.read(_CHUNK), b""):
            h.update(chunk)
    return h.hexdigest()


def _pg_params() -> dict:
    u = urlparse(settings.DATABASE_URL.replace("+asyncpg", "").replace("+psycopg2", ""))
    return {
        "host": u.hostname or "localhost",
        "port": u.port or 5432,
        "db": (u.path or "").lstrip("/") or settings.POSTGRES_DB,
        "user": u.username or settings.POSTGRES_USER,
        "password": u.password or settings.POSTGRES_PASSWORD,
    }


def _set_perms(path: Path) -> None:
    try:
        os.chmod(path, 0o600)
    except OSError:  # pragma: no cover - non-POSIX / permission edge
        pass


# ==========================================================================
# Job lifecycle
# ==========================================================================
async def create_backup_job(session: AsyncSession, backup_type: str, admin_id: int | None = None) -> BackupJob:
    if backup_type not in BACKUP_TYPES:
        raise BackupError(f"unknown backup type: {backup_type}")
    job = BackupJob(backup_type=backup_type, status="pending", created_by_admin_id=admin_id)
    session.add(job)
    await session.commit()
    await session.refresh(job)
    return job


async def get_backup(session: AsyncSession, job_id: int) -> BackupJob | None:
    return await session.get(BackupJob, job_id)


async def list_backups(session: AsyncSession, *, status: str | None = None,
                       backup_type: str | None = None, limit: int = 50, offset: int = 0) -> list[BackupJob]:
    stmt = select(BackupJob)
    if status:
        stmt = stmt.where(BackupJob.status == status)
    if backup_type:
        stmt = stmt.where(BackupJob.backup_type == backup_type)
    stmt = stmt.order_by(BackupJob.id.desc()).limit(limit).offset(offset)
    return list((await session.execute(stmt)).scalars().all())


async def run_backup_job(session: AsyncSession, job_id: int) -> BackupJob | None:
    """Execute a pending job: mark running, produce the file, mark completed/failed.

    Never raises for an expected backup failure — the error is scrubbed and
    recorded on the job. A failure removes only its own partial file; existing
    backups are untouched.
    """
    job = await get_backup(session, job_id)
    if job is None:
        return None
    job.status = "running"
    job.started_at = _now()
    await session.commit()
    try:
        if job.backup_type == "database":
            await create_database_backup(session, job_id)
        elif job.backup_type == "storage":
            await create_storage_backup(session, job_id)
        elif job.backup_type == "full":
            await create_full_backup(session, job_id)
        else:  # pragma: no cover - guarded at creation
            raise BackupError(f"unknown backup type: {job.backup_type}")
        job = await get_backup(session, job_id)
        job.status = "completed"
        job.completed_at = _now()
        job.error_message = None
        await session.commit()
    except Exception as exc:  # noqa: BLE001 - record, never crash the caller
        await session.rollback()
        job = await get_backup(session, job_id)
        if job is not None:
            job.status = "failed"
            job.failed_at = _now()
            job.error_message = _scrub(str(exc))[:1000]
            await session.commit()
        log.warning("backup job %s failed: %s", job_id, _scrub(str(exc)))
    return job


# ==========================================================================
# Producers
# ==========================================================================
def _finalize(job: BackupJob, dest: Path, meta: dict) -> None:
    _set_perms(dest)
    job.file_path = _rel(dest)
    job.file_name = dest.name
    job.file_size = dest.stat().st_size
    job.checksum_sha256 = calculate_sha256(dest)
    job.metadata_json = json.dumps(meta, ensure_ascii=False)


async def _logical_sql_dump(session: AsyncSession) -> str:
    """Dialect-neutral row dump (fallback for SQLite/dev). Not for production."""
    from app.models import Base
    lines = [
        "-- DigitalCore logical fallback dump (data only).",
        "-- Production database backups MUST use pg_dump; this fallback exists",
        "-- for dev/test environments without pg_dump.",
    ]
    # Unsorted: a data-only dump does not need FK order, and sorted_tables warns
    # on the (intentional) orders<->v2ray_services cycle.
    for table in Base.metadata.tables.values():
        rows = (await session.execute(table.select())).mappings().all()
        if not rows:
            continue
        cols = list(rows[0].keys())
        collist = ", ".join(cols)
        for r in rows:
            vals = ", ".join(_sql_literal(r[c]) for c in cols)
            lines.append(f"INSERT INTO {table.name} ({collist}) VALUES ({vals});")
    return "\n".join(lines) + "\n"


def _sql_literal(v: object) -> str:
    if v is None:
        return "NULL"
    if isinstance(v, bool):
        return "1" if v else "0"
    if isinstance(v, (int, float)):
        return str(v)
    s = str(v).replace("'", "''")
    return f"'{s}'"


async def _pg_dump_to(path: Path) -> None:
    """Stream ``pg_dump`` stdout into a gzip file. Password via PGPASSWORD only."""
    p = _pg_params()
    env = {**os.environ, "PGPASSWORD": p["password"]}
    cmd = [
        "pg_dump", "-h", str(p["host"]), "-p", str(p["port"]), "-U", str(p["user"]),
        "-d", str(p["db"]), "--no-owner", "--no-privileges", "--clean", "--if-exists",
    ]
    proc = await asyncio.create_subprocess_exec(
        *cmd, stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.PIPE, env=env,
    )

    def _consume(reader_bytes: bytes) -> None:
        with gzip.open(path, "wb") as gz:
            gz.write(reader_bytes)

    stdout, stderr = await proc.communicate()
    if proc.returncode != 0:
        raise BackupError(_scrub("pg_dump failed: " + (stderr.decode("utf-8", "replace")[:300])))
    await asyncio.to_thread(_consume, stdout)


async def _make_db_gz(session: AsyncSession, dest: Path) -> str:
    """Write a gzipped SQL dump to ``dest``; return the tool used."""
    dialect = session.bind.dialect.name if session.bind else "unknown"
    if dialect.startswith("postgres") and shutil.which("pg_dump"):
        await _pg_dump_to(dest)
        return "pg_dump"
    sql = await _logical_sql_dump(session)
    await asyncio.to_thread(lambda: _write_gz(dest, sql.encode("utf-8")))
    return "sqlalchemy-fallback"


def _write_gz(dest: Path, data: bytes) -> None:
    with gzip.open(dest, "wb") as gz:
        gz.write(data)


def _add_storage_dirs(tar: tarfile.TarFile) -> list[str]:
    included = []
    for sub in STORAGE_SUBDIRS:
        p = STORAGE_ROOT / sub
        if p.is_dir():
            tar.add(p, arcname=f"storage/{sub}")
            included.append(sub)
    return included


async def create_database_backup(session: AsyncSession, job_id: int) -> BackupJob:
    job = await get_backup(session, job_id)
    if job is None:
        raise BackupError("job not found")
    dest = _dated_dir() / f"digitalcore-db-{_timestamp()}.sql.gz"
    try:
        tool = await _make_db_gz(session, dest)
        _finalize(job, dest, {"tool": tool, "kind": "database"})
    except Exception:
        dest.unlink(missing_ok=True)
        raise
    return job


async def create_storage_backup(session: AsyncSession, job_id: int) -> BackupJob:
    job = await get_backup(session, job_id)
    if job is None:
        raise BackupError("job not found")
    dest = _dated_dir() / f"digitalcore-storage-{_timestamp()}.tar.gz"

    def _build() -> list[str]:
        with tarfile.open(dest, "w:gz") as tar:
            return _add_storage_dirs(tar)

    try:
        included = await asyncio.to_thread(_build)
        _finalize(job, dest, {"kind": "storage", "included": included})
    except Exception:
        dest.unlink(missing_ok=True)
        raise
    return job


async def create_full_backup(session: AsyncSession, job_id: int) -> BackupJob:
    job = await get_backup(session, job_id)
    if job is None:
        raise BackupError("job not found")
    dest = _dated_dir() / f"digitalcore-full-{_timestamp()}.tar.gz"
    tmpdir = Path(tempfile.mkdtemp(prefix="dc-full-"))
    db_gz = tmpdir / f"digitalcore-db-{_timestamp()}.sql.gz"
    try:
        tool = await _make_db_gz(session, db_gz)
        meta = {
            "kind": "full", "tool": tool, "created_at": _now().isoformat(),
            "version": _app_version(),
        }
        readme = _restore_readme()

        def _build() -> list[str]:
            with tarfile.open(dest, "w:gz") as tar:
                tar.add(db_gz, arcname=f"database/{db_gz.name}")
                included = _add_storage_dirs(tar)
                _add_bytes(tar, "metadata.json",
                           json.dumps({**meta, "included_storage": included},
                                      ensure_ascii=False, indent=2).encode())
                _add_bytes(tar, "RESTORE.txt", readme.encode())
                return included

        included = await asyncio.to_thread(_build)
        _finalize(job, dest, {**meta, "included_storage": included})
    except Exception:
        dest.unlink(missing_ok=True)
        raise
    finally:
        shutil.rmtree(tmpdir, ignore_errors=True)
    return job


def _add_bytes(tar: tarfile.TarFile, name: str, data: bytes) -> None:
    import io
    info = tarfile.TarInfo(name=name)
    info.size = len(data)
    tar.addfile(info, io.BytesIO(data))


def _app_version() -> str:
    try:
        from app import __version__
        return __version__
    except Exception:  # pragma: no cover
        return "unknown"


def _restore_readme() -> str:
    return (
        "DigitalCore full backup\n"
        "=======================\n\n"
        "Contents:\n"
        "  database/digitalcore-db-*.sql.gz   gzipped SQL dump (pg_dump in prod)\n"
        "  storage/<subdir>/                  uploaded files (receipts, tickets, ...)\n"
        "  metadata.json                      backup metadata (no secrets)\n\n"
        "Restore (production, on the server):\n"
        "  1. Copy this archive to the server.\n"
        "  2. Run:  bash scripts/restore.sh /path/to/this-archive.tar.gz\n"
        "     (it asks you to type RESTORE_DIGITALCORE and makes a pre-restore backup)\n\n"
        "Never restore an untrusted backup. Restores overwrite the live database.\n"
    )


# ==========================================================================
# Verify / delete / cleanup
# ==========================================================================
async def verify_backup(session: AsyncSession, job_id: int) -> dict:
    job = await get_backup(session, job_id)
    if job is None:
        return {"ok": False, "reason": "not_found"}
    if job.status == "deleted":
        return {"ok": False, "reason": "deleted"}
    path = _abs_path(job)
    if path is None or not path.exists():
        return {"ok": False, "reason": "missing_file"}
    if not job.checksum_sha256:
        return {"ok": False, "reason": "no_checksum"}
    actual = await asyncio.to_thread(calculate_sha256, path)
    ok = actual == job.checksum_sha256
    return {
        "ok": ok,
        "reason": "checksum_ok" if ok else "checksum_mismatch",
        "expected": job.checksum_sha256,
        "actual": actual,
        "size": path.stat().st_size,
    }


def _delete_file(job: BackupJob) -> bool:
    """Unlink a job's file iff it lives under storage/backups. Returns True if removed."""
    path = _abs_path(job)
    if path is not None and path.exists():
        path.unlink(missing_ok=True)
        return True
    return False


async def delete_backup(session: AsyncSession, job_id: int, admin_id: int | None = None) -> BackupJob | None:
    job = await get_backup(session, job_id)
    if job is None:
        return None
    _delete_file(job)
    job.status = "deleted"
    await session.commit()
    return job


async def cleanup_old_backups(session: AsyncSession, retention_days: int | None = None,
                              keep_last: int | None = None) -> dict:
    """Delete completed backups older than retention, but ALWAYS keep the newest
    ``keep_last`` (>=1) — the single latest successful backup is never removed."""
    svc = SettingsService(session)
    if retention_days is None:
        retention_days = await svc.get_int("backup_retention_days", 7)
    if keep_last is None:
        keep_last = await svc.get_int("backup_keep_last", 5)
    keep_last = max(int(keep_last), 1)

    completed = list((await session.execute(
        select(BackupJob).where(BackupJob.status == "completed")
        .order_by(BackupJob.created_at.desc(), BackupJob.id.desc())
    )).scalars().all())

    removed: list[int] = []
    now = _now()
    for idx, job in enumerate(completed):
        if idx < keep_last:
            continue  # always keep the newest keep_last
        age_days = (now - _aware(job.created_at)).days
        if retention_days and age_days < int(retention_days):
            continue
        _delete_file(job)
        job.status = "deleted"
        removed.append(job.id)
    await session.commit()
    return {"removed": removed, "removed_count": len(removed),
            "kept": len(completed) - len(removed), "keep_last": keep_last,
            "retention_days": int(retention_days or 0)}


# ==========================================================================
# Scheduled maintenance (worker)
# ==========================================================================
async def _has_scheduled_backup_today(session: AsyncSession, now: datetime) -> bool:
    """True if a system (unattended) backup already completed today (UTC)."""
    day_start = now.replace(hour=0, minute=0, second=0, microsecond=0)
    n = await session.scalar(
        select(func.count()).select_from(BackupJob).where(
            BackupJob.status == "completed",
            BackupJob.created_by_admin_id.is_(None),
            BackupJob.created_at >= day_start,
        )
    )
    return bool(n)


async def run_scheduled_maintenance(session: AsyncSession) -> dict:
    """Worker hook: prune old backups, and take a scheduled backup if enabled and
    it is the configured hour and none ran today. Off by default. Never raises
    for expected conditions; the latest successful backup is never deleted."""
    from app.services import audit_service
    svc = SettingsService(session)
    acted: dict = {}
    if not await svc.get_bool("backups_enabled", True):
        return {"acted": False, "skipped": "backups_disabled"}

    cleaned = await cleanup_old_backups(session)
    if cleaned["removed_count"]:
        acted["cleanup_removed"] = cleaned["removed_count"]
        await audit_service.log(
            session, actor_type="system", actor_id=None,
            action="backup_cleanup_completed",
            meta=f"removed={cleaned['removed_count']} kept={cleaned['kept']}",
        )

    if await svc.get_bool("scheduled_backups_enabled", False):
        hour = await svc.get_int("scheduled_backup_hour", 3)
        now = _now()
        if now.hour == hour and not await _has_scheduled_backup_today(session, now):
            btype = await svc.get_str("scheduled_backup_type", "full")
            if btype not in BACKUP_TYPES:
                btype = "full"
            job = await create_backup_job(session, btype, admin_id=None)
            job = await run_backup_job(session, job.id)
            acted["scheduled_backup"] = {"id": (job.id if job else None),
                                         "status": (job.status if job else "unknown")}
    return {"acted": bool(acted), **acted}

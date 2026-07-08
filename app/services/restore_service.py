"""Restore (Phase 12): verified, owner-only, confirmation-gated restore.

**Safety-first design.** Restoring a database is destructive and risky to run
from inside the live web process, so — as the phase spec prefers — the actual
database overwrite is delegated to ``scripts/restore.sh`` on the server. What the
app does provide, safely and testably, is:

  * ``validate_backup_file`` / ``inspect_backup`` — read-only checks (path is
    under ``storage/backups``, gzip magic, archive members) with **no
    extraction of untrusted paths**;
  * ``create_restore_plan`` — a human-readable plan + warnings + the exact CLI
    command, with no side effects;
  * a signed, time-limited **confirmation token** bound to (admin, backup) so a
    restore can never be triggered by a stray/public request;
  * ``restore_*`` entry points that verify the token + the backup checksum,
    take a **pre-restore backup first**, turn on maintenance mode, and then
    either perform the *non-destructive* storage restore in-app or return a
    ``manual_required`` result pointing at the CLI for the destructive DB step.

A restore never deletes existing backup files.
"""
from __future__ import annotations

import gzip
import logging
import shutil
import tarfile
import tempfile
from pathlib import Path

from itsdangerous import BadSignature, SignatureExpired, URLSafeTimedSerializer
from sqlalchemy.ext.asyncio import AsyncSession

from app.config import settings
from app.core.settings_service import SettingsService
from app.services import backup_service
from app.services.backup_service import (
    BACKUPS_ROOT,
    STORAGE_ROOT,
    _abs_path,
    _within,
)

log = logging.getLogger("restore")

# Typed confirmation phrase (also required by scripts/restore.sh).
RESTORE_SENTINEL = "RESTORE_DIGITALCORE"
_TOKEN_SALT = "digitalcore.restore.v1"
_TOKEN_MAX_AGE = 600  # seconds


class RestoreError(Exception):
    """Restore problem; messages carry no secrets."""


def _serializer() -> URLSafeTimedSerializer:
    secret = settings.JWT_SECRET or settings.SECRET_KEY or "change_me"
    return URLSafeTimedSerializer(secret, salt=_TOKEN_SALT)


# ==========================================================================
# Read-only inspection
# ==========================================================================
def validate_backup_file(path: str | Path) -> dict:
    """Confirm ``path`` is a real gzip backup living under storage/backups."""
    p = Path(path).resolve()
    if not _within(p, BACKUPS_ROOT):
        return {"ok": False, "reason": "outside_backup_dir"}
    if not p.is_file():
        return {"ok": False, "reason": "not_found"}
    try:
        with open(p, "rb") as f:
            magic = f.read(2)
    except OSError:
        return {"ok": False, "reason": "unreadable"}
    if magic != b"\x1f\x8b":  # gzip
        return {"ok": False, "reason": "not_gzip"}
    return {"ok": True, "reason": "ok", "size": p.stat().st_size}


def inspect_backup(path: str | Path) -> dict:
    """Describe a backup's kind + members without extracting anything."""
    v = validate_backup_file(path)
    if not v["ok"]:
        return v
    p = Path(path).resolve()
    name = p.name
    if tarfile.is_tarfile(p):
        with tarfile.open(p, "r:gz") as tar:
            members = [m.name for m in tar.getmembers() if m.isfile()]
        has_db = any(m.startswith("database/") for m in members)
        has_storage = any(m.startswith("storage/") for m in members)
        kind = "full" if has_db else "storage"
        return {
            "ok": True, "reason": "ok", "kind": kind, "size": p.stat().st_size,
            "has_database": has_db, "has_storage": has_storage,
            "member_count": len(members), "members": members[:200],
        }
    # plain gzip (not a tar) → a database dump
    return {"ok": True, "reason": "ok", "kind": "database", "size": p.stat().st_size,
            "has_database": name.endswith(".sql.gz"), "has_storage": False}


# ==========================================================================
# Confirmation token
# ==========================================================================
def generate_restore_confirm_token(admin_id: int, backup_job_id: int) -> str:
    return _serializer().dumps({"a": int(admin_id), "b": int(backup_job_id)})


def verify_restore_confirm_token(token: str, admin_id: int, backup_job_id: int) -> bool:
    if not token:
        return False
    try:
        data = _serializer().loads(token, max_age=_TOKEN_MAX_AGE)
    except (SignatureExpired, BadSignature):
        return False
    return data.get("a") == int(admin_id) and data.get("b") == int(backup_job_id)


# ==========================================================================
# Plan
# ==========================================================================
async def create_restore_plan(session: AsyncSession, backup_job_id: int) -> dict:
    """A read-only plan: no mutation, no token, no side effects."""
    job = await backup_service.get_backup(session, backup_job_id)
    if job is None:
        return {"ok": False, "reason": "not_found"}
    if job.status == "deleted":
        return {"ok": False, "reason": "deleted"}
    path = _abs_path(job)
    if path is None or not path.exists():
        return {"ok": False, "reason": "missing_file"}

    verify = await backup_service.verify_backup(session, backup_job_id)
    info = inspect_backup(path)
    steps = [
        "A fresh pre-restore backup is taken first (your current data is saved).",
        "Maintenance mode is enabled so users cannot write during the restore.",
        "Storage files are restored in-app; the database is restored via the CLI.",
        "Maintenance mode stays on until you verify the app and turn it off.",
    ]
    warnings = [
        "Restoring OVERWRITES the current database — this cannot be undone except "
        "from the pre-restore backup.",
        "Only run restores from backups you trust.",
    ]
    cli = f"bash scripts/restore.sh {job.file_path}"
    return {
        "ok": verify.get("ok", False),
        "reason": verify.get("reason"),
        "backup": {"id": job.id, "type": job.backup_type, "file_name": job.file_name,
                   "size": job.file_size, "checksum": job.checksum_sha256},
        "inspect": info,
        "checksum_ok": verify.get("ok", False),
        "steps": steps,
        "warnings": warnings,
        "cli_command": cli,
        "sentinel": RESTORE_SENTINEL,
    }


# ==========================================================================
# Guarded restore entry points
# ==========================================================================
async def _preflight(session: AsyncSession, backup_job_id: int, confirm_token: str,
                     admin_id: int) -> tuple:
    """Shared restore guard: token + checksum + pre-restore backup + maintenance.

    Returns (job, pre_restore_job). Raises RestoreError on any failure — and on
    failure NO existing backup is touched.
    """
    job = await backup_service.get_backup(session, backup_job_id)
    if job is None or job.status == "deleted":
        raise RestoreError("backup not available")
    if not verify_restore_confirm_token(confirm_token, admin_id, backup_job_id):
        raise RestoreError("invalid or expired confirmation token")
    verify = await backup_service.verify_backup(session, backup_job_id)
    if not verify.get("ok"):
        raise RestoreError(f"backup verification failed: {verify.get('reason')}")

    # Pre-restore safety backup FIRST.
    pre = await backup_service.create_backup_job(session, "full", admin_id=admin_id)
    pre = await backup_service.run_backup_job(session, pre.id)
    if pre is None or pre.status != "completed":
        raise RestoreError("pre-restore backup failed; aborting restore")

    # Enable maintenance mode (best-effort).
    try:
        await SettingsService(session).set("maintenance_mode", "true")
        await session.commit()
    except Exception:  # noqa: BLE001 - never block the restore on this
        await session.rollback()
    return job, pre


async def restore_storage_from_backup(session: AsyncSession, backup_job_id: int,
                                      confirm_token: str, *, admin_id: int) -> dict:
    """Non-destructive storage restore, performed safely in-app."""
    job, pre = await _preflight(session, backup_job_id, confirm_token, admin_id)
    path = _abs_path(job)
    restored = _extract_storage(path)
    return {"status": "completed", "restored_dirs": restored,
            "pre_restore_backup_id": pre.id}


async def restore_database_from_backup(session: AsyncSession, backup_job_id: int,
                                       confirm_token: str, *, admin_id: int) -> dict:
    """Database restore is delegated to the CLI (safer than a live web overwrite)."""
    job, pre = await _preflight(session, backup_job_id, confirm_token, admin_id)
    return {
        "status": "manual_required",
        "message": "Pre-restore backup done and maintenance mode is on. Run the "
                   "CLI on the server to complete the database restore.",
        "cli_command": f"bash scripts/restore.sh {job.file_path}",
        "pre_restore_backup_id": pre.id,
    }


async def restore_full_backup(session: AsyncSession, backup_job_id: int,
                              confirm_token: str, *, admin_id: int) -> dict:
    """Full restore: storage is restored in-app, the DB step is delegated to CLI."""
    job, pre = await _preflight(session, backup_job_id, confirm_token, admin_id)
    path = _abs_path(job)
    restored = _extract_storage(path)
    return {
        "status": "manual_required",
        "message": "Storage restored and pre-restore backup done; maintenance "
                   "mode is on. Run the CLI to complete the database restore.",
        "restored_dirs": restored,
        "cli_command": f"bash scripts/restore.sh {job.file_path}",
        "pre_restore_backup_id": pre.id,
    }


def _extract_storage(archive: Path | None) -> list[str]:
    """Extract only ``storage/<subdir>/`` members into a temp dir, then copy into
    place. Rejects any member whose path escapes the staging dir (Zip-Slip /
    tar traversal guard). Returns the restored subdir names."""
    if archive is None or not tarfile.is_tarfile(archive):
        return []
    staging = Path(tempfile.mkdtemp(prefix="dc-restore-"))
    restored: list[str] = []
    try:
        with tarfile.open(archive, "r:gz") as tar:
            safe = []
            for m in tar.getmembers():
                if not m.name.startswith("storage/"):
                    continue
                target = (staging / m.name).resolve()
                if not _within(target, staging):
                    raise RestoreError("unsafe path in archive")
                safe.append(m)
            tar.extractall(staging, members=safe)  # noqa: S202 - members path-checked above
        src_storage = staging / "storage"
        if src_storage.is_dir():
            for sub in backup_service.STORAGE_SUBDIRS:
                s = src_storage / sub
                if s.is_dir():
                    dest = STORAGE_ROOT / sub
                    dest.mkdir(parents=True, exist_ok=True)
                    shutil.copytree(s, dest, dirs_exist_ok=True)
                    restored.append(sub)
    finally:
        shutil.rmtree(staging, ignore_errors=True)
    return restored

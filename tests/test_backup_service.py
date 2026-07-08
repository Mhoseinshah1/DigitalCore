"""Phase 12 backup_service: create/run/verify/cleanup/delete + no-secret errors."""
from __future__ import annotations

import tarfile
from datetime import datetime, timedelta, timezone
from pathlib import Path

import pytest_asyncio
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker, create_async_engine
from sqlalchemy.pool import StaticPool

from app.models import Base, BackupJob, User
from app.services import backup_service as B

NOW = datetime.now(timezone.utc)


@pytest_asyncio.fixture
async def db(monkeypatch, tmp_path):
    # Redirect all backup paths into a temp dir so tests never touch repo storage.
    repo = tmp_path
    storage = repo / "storage"
    (storage / "receipts").mkdir(parents=True)
    (storage / "receipts" / "a.txt").write_text("hi")
    monkeypatch.setattr(B, "REPO_ROOT", repo)
    monkeypatch.setattr(B, "STORAGE_ROOT", storage)
    monkeypatch.setattr(B, "BACKUPS_ROOT", storage / "backups")

    engine = create_async_engine("sqlite+aiosqlite://", poolclass=StaticPool,
                                 connect_args={"check_same_thread": False})
    async with engine.begin() as conn:
        await conn.run_sync(Base.metadata.create_all)
    maker = async_sessionmaker(engine, class_=AsyncSession, expire_on_commit=False)
    async with maker() as s:
        s.add(User(telegram_id=1, first_name="A"))
        await s.commit()
    try:
        yield maker
    finally:
        await engine.dispose()


async def _run(maker, backup_type):
    async with maker() as s:
        job = await B.create_backup_job(s, backup_type, admin_id=1)
        return await B.run_backup_job(s, job.id)


async def test_create_backup_job(db) -> None:
    async with db() as s:
        job = await B.create_backup_job(s, "full", admin_id=1)
    assert job.id and job.status == "pending" and job.backup_type == "full"


async def test_run_database_backup(db) -> None:
    job = await _run(db, "database")
    assert job.status == "completed"
    assert job.file_name.startswith("digitalcore-db-") and job.file_name.endswith(".sql.gz")
    assert job.file_size > 0 and len(job.checksum_sha256) == 64
    assert (B.REPO_ROOT / job.file_path).exists()


async def test_run_storage_backup(db) -> None:
    job = await _run(db, "storage")
    assert job.status == "completed" and job.file_name.endswith(".tar.gz")
    with tarfile.open(B.REPO_ROOT / job.file_path) as tar:
        assert any(n.startswith("storage/receipts") for n in tar.getnames())


async def test_full_backup_archive(db) -> None:
    job = await _run(db, "full")
    assert job.status == "completed"
    with tarfile.open(B.REPO_ROOT / job.file_path) as tar:
        names = tar.getnames()
    assert any(n.startswith("database/") for n in names)
    assert "metadata.json" in names and "RESTORE.txt" in names


async def test_sha256_matches_file(db) -> None:
    job = await _run(db, "storage")
    assert B.calculate_sha256(B.REPO_ROOT / job.file_path) == job.checksum_sha256


async def test_verify_success_and_failure(db) -> None:
    job = await _run(db, "storage")
    async with db() as s:
        assert (await B.verify_backup(s, job.id))["ok"] is True
        # corrupt the file → checksum mismatch
        (B.REPO_ROOT / job.file_path).write_bytes(b"corrupt")
        v = await B.verify_backup(s, job.id)
        assert v["ok"] is False and v["reason"] == "checksum_mismatch"


async def test_verify_missing_file(db) -> None:
    job = await _run(db, "storage")
    (B.REPO_ROOT / job.file_path).unlink()
    async with db() as s:
        v = await B.verify_backup(s, job.id)
    assert v["ok"] is False and v["reason"] == "missing_file"


async def test_delete_removes_file_and_marks_deleted(db) -> None:
    job = await _run(db, "storage")
    path = B.REPO_ROOT / job.file_path
    assert path.exists()
    async with db() as s:
        await B.delete_backup(s, job.id, admin_id=1)
        again = await B.get_backup(s, job.id)
    assert again.status == "deleted" and not path.exists()


async def test_cleanup_keeps_latest(db) -> None:
    async with db() as s:
        base = NOW - timedelta(days=40)
        for i in range(8):
            s.add(BackupJob(backup_type="full", status="completed",
                            created_at=base + timedelta(seconds=i)))
        await s.commit()
        res = await B.cleanup_old_backups(s, retention_days=7, keep_last=5)
    assert res["removed_count"] == 3 and res["kept"] == 5


async def test_cleanup_never_deletes_the_single_latest(db) -> None:
    async with db() as s:
        s.add(BackupJob(backup_type="full", status="completed",
                        created_at=NOW - timedelta(days=100)))
        await s.commit()
        res = await B.cleanup_old_backups(s, retention_days=1, keep_last=1)
    assert res["removed_count"] == 0


async def test_error_message_is_scrubbed() -> None:
    # The scrubber removes URL credentials and the configured DB password.
    dirty = "could not connect postgresql://u:supersecret@host/db"
    clean = B._scrub(dirty)
    assert "supersecret" not in clean and ":***@" in clean


async def test_backup_path_stays_under_backups_root(db) -> None:
    job = await _run(db, "storage")
    p = B._abs_path(job)
    assert p is not None and str(p).startswith(str(B.BACKUPS_ROOT.resolve()))
    # a job pointing outside is rejected
    async with db() as s:
        bad = await B.get_backup(s, job.id)
        bad.file_path = "../../etc/passwd"
        assert B._abs_path(bad) is None

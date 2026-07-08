"""Phase 12 worker: scheduled backups off by default; cleanup keeps latest."""
from __future__ import annotations

from datetime import datetime, timedelta, timezone

import pytest_asyncio
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker, create_async_engine
from sqlalchemy.pool import StaticPool

from app.core.settings_service import SettingsService
from app.models import Base, BackupJob
from app.services import backup_service as B

NOW = datetime.now(timezone.utc)


@pytest_asyncio.fixture
async def db(monkeypatch, tmp_path):
    storage = tmp_path / "storage"
    (storage / "exports").mkdir(parents=True)
    monkeypatch.setattr(B, "REPO_ROOT", tmp_path)
    monkeypatch.setattr(B, "STORAGE_ROOT", storage)
    monkeypatch.setattr(B, "BACKUPS_ROOT", storage / "backups")
    engine = create_async_engine("sqlite+aiosqlite://", poolclass=StaticPool,
                                 connect_args={"check_same_thread": False})
    async with engine.begin() as conn:
        await conn.run_sync(Base.metadata.create_all)
    maker = async_sessionmaker(engine, class_=AsyncSession, expire_on_commit=False)
    try:
        yield maker
    finally:
        await engine.dispose()


async def test_scheduled_backup_disabled_by_default(db) -> None:
    async with db() as s:
        result = await B.run_scheduled_maintenance(s)
    assert "scheduled_backup" not in result


async def test_scheduled_backup_runs_when_enabled_at_hour(db) -> None:
    async with db() as s:
        svc = SettingsService(s)
        await svc.set("scheduled_backups_enabled", "true", audit=False)
        await svc.set("scheduled_backup_type", "storage", audit=False)
        await svc.set("scheduled_backup_hour", str(NOW.hour), audit=False)
        await s.commit()
        result = await B.run_scheduled_maintenance(s)
        assert result["scheduled_backup"]["status"] == "completed"
        # a second run the same day must not create a duplicate
        result2 = await B.run_scheduled_maintenance(s)
        assert "scheduled_backup" not in result2


async def test_maintenance_cleanup_keeps_latest(db) -> None:
    async with db() as s:
        base = NOW - timedelta(days=60)
        for i in range(6):
            s.add(BackupJob(backup_type="full", status="completed",
                            created_at=base + timedelta(seconds=i)))
        await s.commit()
        # tighten retention/keep via settings, then run the worker hook
        svc = SettingsService(s)
        await svc.set("backup_retention_days", "7", audit=False)
        await svc.set("backup_keep_last", "2", audit=False)
        await s.commit()
        result = await B.run_scheduled_maintenance(s)
        remaining = [b for b in await B.list_backups(s, status="completed")]
    assert result.get("cleanup_removed") == 4
    assert len(remaining) == 2  # newest two always kept


async def test_disabled_backups_skips_everything(db) -> None:
    async with db() as s:
        await SettingsService(s).set("backups_enabled", "false", audit=False)
        await s.commit()
        result = await B.run_scheduled_maintenance(s)
    assert result == {"acted": False, "skipped": "backups_disabled"}

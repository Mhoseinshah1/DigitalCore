"""Phase 12 restore_service: inspection, token gating, traversal, pre-restore backup."""
from __future__ import annotations

import pytest_asyncio
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker, create_async_engine
from sqlalchemy.pool import StaticPool

from app.core.settings_service import SettingsService
from app.models import Base, User
from app.services import backup_service as B
from app.services import restore_service as R


@pytest_asyncio.fixture
async def db(monkeypatch, tmp_path):
    repo = tmp_path
    storage = repo / "storage"
    (storage / "exports").mkdir(parents=True)
    (storage / "exports" / "rep.csv").write_text("c\n1")
    monkeypatch.setattr(B, "REPO_ROOT", repo)
    monkeypatch.setattr(B, "STORAGE_ROOT", storage)
    monkeypatch.setattr(B, "BACKUPS_ROOT", storage / "backups")
    # restore_service imported these names at import time — repoint them too.
    monkeypatch.setattr(R, "BACKUPS_ROOT", storage / "backups")
    monkeypatch.setattr(R, "STORAGE_ROOT", storage)

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


async def _make_full(maker):
    async with maker() as s:
        job = await B.create_backup_job(s, "full", admin_id=7)
        job = await B.run_backup_job(s, job.id)
        return job.id, job.file_path


async def test_token_is_bound_to_admin_and_backup(db) -> None:
    tok = R.generate_restore_confirm_token(7, 123)
    assert R.verify_restore_confirm_token(tok, 7, 123) is True
    assert R.verify_restore_confirm_token(tok, 8, 123) is False
    assert R.verify_restore_confirm_token(tok, 7, 124) is False
    assert R.verify_restore_confirm_token("garbage", 7, 123) is False


async def test_validate_rejects_paths_outside_backups(db) -> None:
    assert R.validate_backup_file("/etc/passwd")["ok"] is False
    assert R.validate_backup_file(B.REPO_ROOT / "nope.tar.gz")["ok"] is False


async def test_inspect_full_backup(db) -> None:
    _, fpath = await _make_full(db)
    info = R.inspect_backup(B.REPO_ROOT / fpath)
    assert info["ok"] and info["kind"] == "full"
    assert info["has_database"] and info["has_storage"]


async def test_restore_requires_valid_token(db) -> None:
    jid, _ = await _make_full(db)
    async with db() as s:
        try:
            await R.restore_full_backup(s, jid, "bad-token", admin_id=7)
            assert False, "should have raised"
        except R.RestoreError as exc:
            assert "confirmation token" in str(exc)


async def test_restore_creates_pre_restore_backup_and_maintenance(db) -> None:
    jid, _ = await _make_full(db)
    tok = R.generate_restore_confirm_token(7, jid)
    async with db() as s:
        before = len(await B.list_backups(s))
        result = await R.restore_full_backup(s, jid, tok, admin_id=7)
        after = len(await B.list_backups(s))
        assert result["status"] == "manual_required"
        assert result["pre_restore_backup_id"]
        assert after == before + 1  # pre-restore backup made
        assert "exports" in result["restored_dirs"]
        assert await SettingsService(s).get_bool("maintenance_mode", False) is True


async def test_plan_has_cli_command_and_sentinel(db) -> None:
    jid, _ = await _make_full(db)
    async with db() as s:
        plan = await R.create_restore_plan(s, jid)
    assert plan["ok"] and plan["sentinel"] == "RESTORE_DIGITALCORE"
    assert plan["cli_command"].startswith("bash scripts/restore.sh")

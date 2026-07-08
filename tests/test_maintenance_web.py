"""Phase 12 web: maintenance RBAC, backup CRUD, download safety, restore, audit."""
from __future__ import annotations

import re
from collections.abc import AsyncIterator, Awaitable, Callable

import httpx
import pytest_asyncio
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker, create_async_engine
from sqlalchemy.pool import StaticPool

from app.core.security import hash_password
from app.database import get_session
from app.models import Admin, AuditLog, Base
from app.services import backup_service as B
from app.services import restore_service as R
from app.web.main import app

PASSWORD = "maint-web-1"
ClientFactory = Callable[[str], Awaitable[httpx.AsyncClient]]


@pytest_asyncio.fixture
async def env(monkeypatch, tmp_path) -> AsyncIterator[tuple[ClientFactory, async_sessionmaker]]:
    repo = tmp_path
    storage = repo / "storage"
    (storage / "receipts").mkdir(parents=True)
    (storage / "receipts" / "a.txt").write_text("hi")
    for mod, name, val in (
        (B, "REPO_ROOT", repo), (B, "STORAGE_ROOT", storage),
        (B, "BACKUPS_ROOT", storage / "backups"),
        (R, "BACKUPS_ROOT", storage / "backups"), (R, "STORAGE_ROOT", storage),
    ):
        monkeypatch.setattr(mod, name, val)

    engine = create_async_engine("sqlite+aiosqlite://", poolclass=StaticPool,
                                 connect_args={"check_same_thread": False})
    async with engine.begin() as conn:
        await conn.run_sync(Base.metadata.create_all)
    maker = async_sessionmaker(engine, class_=AsyncSession, expire_on_commit=False)

    async def _override():
        async with maker() as s:
            yield s

    app.dependency_overrides[get_session] = _override
    transport = httpx.ASGITransport(app=app)
    clients: list[httpx.AsyncClient] = []

    async def factory(role: str) -> httpx.AsyncClient:
        un = f"mw_{role}"
        async with maker() as s:
            s.add(Admin(username=un, password_hash=hash_password(PASSWORD), is_active=True,
                        is_super_admin=(role == "owner"), role=role))
            await s.commit()
        c = httpx.AsyncClient(transport=transport, base_url="http://testserver")
        clients.append(c)
        r = await c.post("/admin/login", data={"username": un, "password": PASSWORD},
                         follow_redirects=False)
        assert r.status_code == 302
        return c

    try:
        yield factory, maker
    finally:
        for c in clients:
            await c.aclose()
        app.dependency_overrides.pop(get_session, None)
        await engine.dispose()


async def _make_backup(owner) -> None:
    r = await owner.post("/admin/maintenance/backups/create",
                         data={"backup_type": "full"}, follow_redirects=False)
    assert r.status_code == 303 and "saved=created" in r.headers["location"]


# --- auth + pages ---------------------------------------------------------
async def test_maintenance_requires_auth(env) -> None:
    transport = httpx.ASGITransport(app=app)
    async with httpx.AsyncClient(transport=transport, base_url="http://testserver") as anon:
        for p in ("/admin/maintenance", "/admin/maintenance/backups",
                  "/admin/maintenance/health"):
            r = await anon.get(p, follow_redirects=False)
            assert r.status_code in (302, 307), (p, r.status_code)


async def test_owner_sees_all_maintenance_pages(env) -> None:
    factory, _ = env
    owner = await factory("owner")
    for p in ("/admin/maintenance", "/admin/maintenance/backups",
              "/admin/maintenance/restore", "/admin/maintenance/health",
              "/admin/maintenance/system-info"):
        assert (await owner.get(p)).status_code == 200, p


# --- RBAC -----------------------------------------------------------------
async def test_admin_backups_but_not_restore(env) -> None:
    factory, _ = env
    admin = await factory("admin")
    assert (await admin.get("/admin/maintenance/backups")).status_code == 200
    assert (await admin.get("/admin/maintenance/restore", follow_redirects=False)).status_code == 403


async def test_support_health_only(env) -> None:
    factory, _ = env
    sup = await factory("support")
    assert (await sup.get("/admin/maintenance/health")).status_code == 200
    assert (await sup.get("/admin/maintenance/backups", follow_redirects=False)).status_code == 403


async def test_viewer_no_maintenance(env) -> None:
    factory, _ = env
    viewer = await factory("viewer")
    assert (await viewer.get("/admin/maintenance", follow_redirects=False)).status_code == 403


# --- backup lifecycle -----------------------------------------------------
async def test_create_verify_download_delete(env) -> None:
    factory, maker = env
    owner = await factory("owner")
    await _make_backup(owner)
    async with maker() as s:
        job = (await B.list_backups(s, status="completed"))[0]
        jid = job.id
    # verify
    r = await owner.post(f"/admin/maintenance/backups/{jid}/verify", follow_redirects=False)
    assert r.status_code == 303 and "verify=checksum_ok" in r.headers["location"]
    # download: no-store, attachment, content
    r = await owner.get(f"/admin/maintenance/backups/{jid}/download")
    assert r.status_code == 200
    assert r.headers["cache-control"] == "private, no-store"
    assert "attachment" in r.headers["content-disposition"] and len(r.content) > 0
    # delete → then download 404
    assert (await owner.post(f"/admin/maintenance/backups/{jid}/delete",
                             follow_redirects=False)).status_code == 303
    assert (await owner.get(f"/admin/maintenance/backups/{jid}/download")).status_code == 404


async def test_accountant_cannot_download(env) -> None:
    factory, maker = env
    owner = await factory("owner")
    await _make_backup(owner)
    async with maker() as s:
        jid = (await B.list_backups(s, status="completed"))[0].id
    acc = await factory("accountant")
    assert (await acc.get(f"/admin/maintenance/backups/{jid}/download",
                          follow_redirects=False)).status_code == 403


async def test_download_rejects_traversal_job(env) -> None:
    # A tampered file_path escaping storage/backups must yield 404, not a file.
    factory, maker = env
    owner = await factory("owner")
    await _make_backup(owner)
    async with maker() as s:
        job = (await B.list_backups(s, status="completed"))[0]
        job.file_path = "../../../../etc/passwd"
        await s.commit()
        jid = job.id
    assert (await owner.get(f"/admin/maintenance/backups/{jid}/download")).status_code == 404


# --- restore --------------------------------------------------------------
async def test_restore_plan_and_confirm(env) -> None:
    factory, maker = env
    owner = await factory("owner")
    await _make_backup(owner)
    async with maker() as s:
        jid = (await B.list_backups(s, status="completed"))[0].id
    r = await owner.post("/admin/maintenance/restore/plan", data={"backup_id": jid})
    assert r.status_code == 200 and "RESTORE_DIGITALCORE" in r.text
    tok = re.search(r'name="confirm_token" value="([^"]+)"', r.text).group(1)
    # wrong phrase → bounce
    r = await owner.post("/admin/maintenance/restore/confirm",
                         data={"backup_id": jid, "confirm_token": tok, "confirm_phrase": "x"},
                         follow_redirects=False)
    assert r.status_code == 303 and "error=phrase" in r.headers["location"]
    # correct phrase → manual_required
    r = await owner.post("/admin/maintenance/restore/confirm",
                         data={"backup_id": jid, "confirm_token": tok,
                               "confirm_phrase": "RESTORE_DIGITALCORE"})
    assert r.status_code == 200 and "scripts/restore.sh" in r.text


# --- audit ----------------------------------------------------------------
async def test_audit_rows_created(env) -> None:
    factory, maker = env
    owner = await factory("owner")
    await _make_backup(owner)
    async with maker() as s:
        jid = (await B.list_backups(s, status="completed"))[0].id
    await owner.get(f"/admin/maintenance/backups/{jid}/download")
    await owner.get("/admin/maintenance/health")
    async with maker() as s:
        actions = {a.action for a in (await s.execute(select(AuditLog))).scalars().all()}
    for need in ("backup_job_created", "backup_completed", "backup_downloaded",
                 "health_check_viewed"):
        assert need in actions, (need, sorted(actions))

"""Health and readiness endpoints.

/health must return 200 with no DB/Redis dependency. /ready must check both the
database and Redis, returning 503 if either is down. The DB and Redis checks are
monkeypatched so the tests are deterministic without real services.
"""
from __future__ import annotations

from app.web import main as web_main


async def _ok() -> bool:
    return True


async def _down() -> bool:
    return False


async def test_health_ok_without_db_or_redis(client) -> None:
    r = await client.get("/health")
    assert r.status_code == 200
    body = r.json()
    assert body["status"] == "ok"
    assert body["service"] == "DigitalCore API"
    # /health must not report DB/Redis state.
    assert "database" not in body
    assert "redis" not in body


async def test_ready_200_when_db_and_redis_up(client, monkeypatch) -> None:
    monkeypatch.setattr(web_main, "database_ok", _ok)
    monkeypatch.setattr(web_main, "redis_ok", _ok)
    r = await client.get("/ready")
    assert r.status_code == 200
    assert r.json() == {"status": "ready", "database": "ok", "redis": "ok"}


async def test_ready_503_when_redis_down(client, monkeypatch) -> None:
    monkeypatch.setattr(web_main, "database_ok", _ok)
    monkeypatch.setattr(web_main, "redis_ok", _down)
    r = await client.get("/ready")
    assert r.status_code == 503
    body = r.json()
    assert body["status"] == "not ready"
    assert body["database"] == "ok"
    assert body["redis"] == "error"


async def test_ready_503_when_db_down(client, monkeypatch) -> None:
    monkeypatch.setattr(web_main, "database_ok", _down)
    monkeypatch.setattr(web_main, "redis_ok", _ok)
    r = await client.get("/ready")
    assert r.status_code == 503
    body = r.json()
    assert body["status"] == "not ready"
    assert body["database"] == "error"
    assert body["redis"] == "ok"

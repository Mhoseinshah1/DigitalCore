"""DigitalCore backend API.

Surface: liveness (/health), readiness (/ready, checks the database AND Redis),
and minimal admin auth (/api/auth/*). No product features yet.
"""
from __future__ import annotations

import logging

from fastapi import FastAPI
from fastapi.responses import JSONResponse

from app.config import settings
from app.core.logging import configure_logging
from app.core.redis import redis_ok
from app.database import database_ok
from app.web.api import auth as auth_api

configure_logging()
log = logging.getLogger("backend")

app = FastAPI(title=settings.service_name, version=settings.APP_VERSION)

app.include_router(auth_api.router)


@app.get("/health", tags=["meta"])
async def health() -> dict:
    """Liveness check — must succeed without touching the database or Redis."""
    return {
        "status": "ok",
        "service": settings.service_name,
        "version": settings.APP_VERSION,
    }


@app.get("/ready", tags=["meta"])
async def ready() -> JSONResponse:
    """Readiness check — verifies both the database and Redis are reachable."""
    db = await database_ok()
    cache = await redis_ok()
    ready_now = db and cache
    if not ready_now:
        log.warning("Readiness check failed (database=%s, redis=%s).", db, cache)
    return JSONResponse(
        status_code=200 if ready_now else 503,
        content={
            "status": "ready" if ready_now else "not ready",
            "database": "ok" if db else "error",
            "redis": "ok" if cache else "error",
        },
    )

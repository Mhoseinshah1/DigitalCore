"""DigitalCore backend API.

Surface: liveness (/health), readiness (/ready, checks the database AND Redis),
and minimal admin auth (/api/auth/*). No product features yet.
"""
from __future__ import annotations

import logging
from pathlib import Path

from fastapi import FastAPI
from fastapi.responses import JSONResponse
from fastapi.staticfiles import StaticFiles

from app.config import settings
from app.core.logging import configure_logging
from app.core.redis import redis_ok
from app.database import database_ok
from app.web.api import auth as auth_api
from app.web.api import settings as settings_api
from app.web.views import router as views_router

configure_logging()
log = logging.getLogger("backend")

BASE_DIR = Path(__file__).resolve().parent

app = FastAPI(title=settings.service_name, version=settings.APP_VERSION)

# Package-relative so it resolves regardless of the working directory (host or
# container). Templates reference /static/... (see templates/base.html).
app.mount("/static", StaticFiles(directory=BASE_DIR / "static"), name="static")

app.include_router(auth_api.router)
app.include_router(settings_api.router)
app.include_router(views_router)


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

"""DigitalCore backend API.

Phase 1 surface: liveness (/health), readiness (/ready, checks the database), and
minimal admin auth (/api/auth/*). No product features yet.
"""
from __future__ import annotations

import logging

from fastapi import FastAPI
from fastapi.responses import JSONResponse
from sqlalchemy import text

from app.config import settings
from app.database import engine
from app.web.api import auth as auth_api

log = logging.getLogger("backend")

app = FastAPI(title=settings.service_name, version=settings.APP_VERSION)

app.include_router(auth_api.router)


@app.get("/health", tags=["meta"])
async def health() -> dict:
    """Liveness check — must succeed without touching the database."""
    return {
        "status": "ok",
        "service": settings.service_name,
        "version": settings.APP_VERSION,
    }


@app.get("/ready", tags=["meta"])
async def ready() -> JSONResponse:
    """Readiness check — verifies the database connection."""
    try:
        async with engine.connect() as conn:
            await conn.execute(text("SELECT 1"))
    except Exception as exc:  # noqa: BLE001 - any failure means "not ready"
        log.warning("Readiness check failed: %s", exc)
        return JSONResponse(
            status_code=503,
            content={
                "status": "not ready",
                "database": "error",
                "detail": "database connection failed",
            },
        )
    return JSONResponse(
        status_code=200,
        content={"status": "ready", "database": "ok"},
    )

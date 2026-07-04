"""FastAPI application: the admin web panel and its JSON API."""
from __future__ import annotations

import logging
from pathlib import Path

from fastapi import FastAPI
from fastapi.staticfiles import StaticFiles

from app import __version__
from app.config import settings
from app.web.api import auth as auth_api
from app.web.api import settings as settings_api
from app.web.views import router as views_router

BASE_DIR = Path(__file__).resolve().parent

log = logging.getLogger("web")
for _warning in settings.insecure_config_warnings():
    log.warning("INSECURE CONFIG: %s", _warning)

app = FastAPI(title="DigitalCore Admin", version=__version__)

app.mount("/static", StaticFiles(directory=BASE_DIR / "static"), name="static")

app.include_router(auth_api.router)
app.include_router(settings_api.router)
app.include_router(views_router)


@app.get("/healthz", include_in_schema=False)
async def healthz() -> dict[str, str]:
    return {"status": "ok", "version": __version__}


@app.get("/api/status", tags=["meta"])
async def status() -> dict:
    return {
        "app": "DigitalCore",
        "version": __version__,
        "domain": settings.DOMAIN,
        "maintenance_mode": settings.MAINTENANCE_MODE,
    }

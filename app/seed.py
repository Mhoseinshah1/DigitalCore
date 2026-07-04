"""First-boot seeding of default business-settings records.

Idempotently inserts one settings row per catalog entry (app/core/defaults.py)
with its default value, optionally pre-seeded from the environment via each
entry's env_var. Existing rows are never touched, so re-running is safe.

Admin bootstrap is NOT done here — the admin (email + password) is created by
scripts/create_admin.py.

Run with:  python -m app.seed
"""
from __future__ import annotations

import asyncio
import logging
import os

from sqlalchemy import select

from app.core.defaults import DEFAULTS
from app.core.settings_service import SettingsService
from app.database import SessionLocal
from app.models.setting import Setting

logging.basicConfig(level=logging.INFO, format="%(levelname)s %(message)s")
log = logging.getLogger("seed")


async def seed_default_settings(session) -> int:
    result = await session.execute(select(Setting.key))
    existing = {row[0] for row in result.all()}

    svc = SettingsService(session)
    created = 0
    for d in DEFAULTS:
        if d.key in existing:
            continue
        initial = d.default
        if d.env_var:
            env_value = os.environ.get(d.env_var, "")
            if env_value != "":
                initial = env_value
        # SettingsService handles type coercion and encrypts secret values.
        await svc.set(d.key, initial)
        created += 1

    if created:
        log.info("Seeded %d default settings records.", created)
    else:
        log.info("All default settings already present.")
    return created


async def run() -> None:
    async with SessionLocal() as session:
        await seed_default_settings(session)
        await session.commit()
    log.info("Seeding complete.")


if __name__ == "__main__":
    asyncio.run(run())

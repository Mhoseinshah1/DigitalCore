"""First-boot seeding.

Idempotently:
  1. Creates the owner admin from MAIN_ADMIN_TELEGRAM_ID, giving them a web-panel
     login (username + hashed password from WEB_ADMIN_PASSWORD).
  2. Inserts a default settings record for every entry in the catalog with an
     empty/default value (optionally pre-seeded from the environment).

Runs automatically from the web container's entrypoint on every start. Because
it only fills in what is missing, re-running it never clobbers admin-edited data.
"""
from __future__ import annotations

import asyncio
import logging
import os

from sqlalchemy import select

from app.config import settings
from app.core.defaults import DEFAULTS
from app.core.security import hash_password
from app.database import SessionLocal
from app.models.admin import Admin
from app.models.setting import Setting

logging.basicConfig(level=logging.INFO, format="%(levelname)s %(message)s")
log = logging.getLogger("seed")


async def seed_owner_admin(session) -> None:
    if not settings.MAIN_ADMIN_TELEGRAM_ID:
        log.warning("MAIN_ADMIN_TELEGRAM_ID is not set; skipping owner admin seed.")
        return

    result = await session.execute(
        select(Admin).where(Admin.telegram_id == settings.MAIN_ADMIN_TELEGRAM_ID)
    )
    admin = result.scalar_one_or_none()

    # Determine the web password: use the one provided to the installer, else a
    # random one so the account is never left with an empty password.
    web_password = settings.WEB_ADMIN_PASSWORD or ""
    if admin is None:
        if not web_password:
            web_password = os.urandom(16).hex()
            log.warning(
                "No WEB_ADMIN_PASSWORD provided; generated a random owner password. "
                "Reset it from the panel."
            )
        admin = Admin(
            telegram_id=settings.MAIN_ADMIN_TELEGRAM_ID,
            username=settings.WEB_ADMIN_USERNAME or "admin",
            password_hash=hash_password(web_password),
            is_owner=True,
            is_active=True,
        )
        session.add(admin)
        log.info("Created owner admin telegram_id=%s", settings.MAIN_ADMIN_TELEGRAM_ID)
    else:
        # Existing account: never clobber admin-edited state. In particular do NOT
        # force is_active/is_owner back on — an operator must be able to disable a
        # compromised owner and have that survive a restart. Only fill in gaps.
        if not admin.username:
            admin.username = settings.WEB_ADMIN_USERNAME or "admin"
        # Only set the password when one was explicitly supplied and the account
        # has none yet — never silently overwrite an admin-set password.
        if web_password and not admin.password_hash:
            admin.password_hash = hash_password(web_password)
        log.info("Owner admin already present; left existing state untouched.")


async def seed_default_settings(session) -> int:
    result = await session.execute(select(Setting.key))
    existing = {row[0] for row in result.all()}

    created = 0
    for d in DEFAULTS:
        if d.key in existing:
            continue
        initial = d.default
        if d.env_var:
            env_value = os.environ.get(d.env_var, "")
            if env_value != "":
                initial = env_value
        # Secrets would be encrypted here; none of the seeded defaults are secret.
        session.add(
            Setting(
                key=d.key,
                value=initial or "",
                category=d.category,
                value_type=d.value_type,
                is_secret=d.is_secret,
                label=d.label,
                description=d.description,
            )
        )
        created += 1
    if created:
        log.info("Seeded %d default settings records.", created)
    else:
        log.info("All default settings already present.")
    return created


async def run() -> None:
    async with SessionLocal() as session:
        await seed_owner_admin(session)
        await seed_default_settings(session)
        await session.commit()
    log.info("Seeding complete.")


if __name__ == "__main__":
    asyncio.run(run())

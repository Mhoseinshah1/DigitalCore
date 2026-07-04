#!/usr/bin/env python
"""Create (or optionally reset) the super admin from environment variables.

Reads ADMIN_USERNAME and ADMIN_PASSWORD (and optionally ADMIN_EMAIL) from the
environment (.env). Creates a super admin with that username if it does not
exist. If it already exists, the password is only updated when --reset-password
is passed.

Usage (inside the backend container):
    python scripts/create_admin.py
    python scripts/create_admin.py --reset-password
"""
from __future__ import annotations

import argparse
import asyncio
import os
import sys

# Allow running as `python scripts/create_admin.py` from the repo root.
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from sqlalchemy import select  # noqa: E402

from app.config import settings  # noqa: E402
from app.core.security import hash_password  # noqa: E402
from app.database import SessionLocal  # noqa: E402
from app.models.admin import Admin  # noqa: E402


async def create_admin(reset_password: bool) -> int:
    username = (settings.ADMIN_USERNAME or "").strip()
    email = (settings.ADMIN_EMAIL or "").strip() or None
    password = settings.ADMIN_PASSWORD or ""

    if not username or not password:
        print("ERROR: ADMIN_USERNAME and ADMIN_PASSWORD must be set in the environment.")
        return 1

    async with SessionLocal() as session:
        result = await session.execute(select(Admin).where(Admin.username == username))
        admin = result.scalar_one_or_none()

        adopted = False
        if admin is None and email:
            # Legacy row from a pre-username (email-based) install: adopt it by
            # assigning the username instead of inserting a duplicate email.
            result = await session.execute(select(Admin).where(Admin.email == email))
            legacy = result.scalar_one_or_none()
            if legacy is not None:
                if legacy.username and legacy.username != username:
                    print(
                        f"ERROR: {email} already belongs to admin "
                        f"'{legacy.username}' — choose a different ADMIN_EMAIL."
                    )
                    return 1
                legacy.username = username
                admin = legacy
                adopted = True

        if admin is None:
            session.add(
                Admin(
                    username=username,
                    email=email,
                    password_hash=hash_password(password),
                    is_active=True,
                    is_super_admin=True,
                )
            )
            await session.commit()
            print(f"✓ Super admin created: {username}")
            return 0

        if reset_password:
            admin.password_hash = hash_password(password)
            admin.is_active = True
            admin.is_super_admin = True
            if email and not admin.email:
                admin.email = email
            await session.commit()
            print(f"✓ Password reset for existing admin: {username}")
            return 0

        if adopted:
            await session.commit()
            print(f"✓ Existing admin adopted the username: {username}")
            return 0

        print(
            f"• Admin already exists: {username} (no changes made). "
            "Pass --reset-password to update the password."
        )
        return 0


def main() -> None:
    parser = argparse.ArgumentParser(description="Create or reset the DigitalCore super admin.")
    parser.add_argument(
        "--reset-password",
        action="store_true",
        help="Reset the password of an existing admin to ADMIN_PASSWORD.",
    )
    args = parser.parse_args()
    sys.exit(asyncio.run(create_admin(args.reset_password)))


if __name__ == "__main__":
    main()

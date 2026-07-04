#!/usr/bin/env python
"""Create (or optionally reset) the super admin from environment variables.

Reads ADMIN_EMAIL and ADMIN_PASSWORD from the environment (.env). Creates a
super admin if one with that email does not exist. If it already exists, the
password is only updated when --reset-password is passed.

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
    email = (settings.ADMIN_EMAIL or "").strip()
    password = settings.ADMIN_PASSWORD or ""

    if not email or not password:
        print("ERROR: ADMIN_EMAIL and ADMIN_PASSWORD must be set in the environment.")
        return 1

    async with SessionLocal() as session:
        result = await session.execute(select(Admin).where(Admin.email == email))
        admin = result.scalar_one_or_none()

        if admin is None:
            session.add(
                Admin(
                    email=email,
                    password_hash=hash_password(password),
                    is_active=True,
                    is_super_admin=True,
                )
            )
            await session.commit()
            print(f"✓ Super admin created: {email}")
            return 0

        if reset_password:
            admin.password_hash = hash_password(password)
            admin.is_active = True
            await session.commit()
            print(f"✓ Password reset for existing admin: {email}")
            return 0

        print(
            f"• Admin already exists: {email} (no changes made). "
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

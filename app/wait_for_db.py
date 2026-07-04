"""Block until the database accepts connections (used by the entrypoint)."""
from __future__ import annotations

import asyncio
import sys

from sqlalchemy import text

from app.database import engine


async def _wait(timeout: int = 60) -> bool:
    for _ in range(timeout):
        try:
            async with engine.connect() as conn:
                await conn.execute(text("SELECT 1"))
            return True
        except Exception:  # noqa: BLE001 - any connection error means "not ready yet"
            await asyncio.sleep(1)
    return False


def main() -> None:
    ok = asyncio.run(_wait())
    if not ok:
        print("Database did not become ready in time.", file=sys.stderr)
        sys.exit(1)
    print("Database is ready.")


if __name__ == "__main__":
    main()

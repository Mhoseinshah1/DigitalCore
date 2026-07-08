#!/usr/bin/env python
"""Runtime bot diagnostic for DigitalCore.

Run it inside the backend (or bot) container, which already has the app env:

    docker compose exec -T backend python scripts/debug_bot_state.py

It reports whether migration 0019 (product categories) is applied, category /
product counts, and the bot-relevant settings — so a "the bot is not OK in
production" report can be root-caused in one command. It NEVER selects secret
values and is safe to run against a live database (read-only).
"""
from __future__ import annotations

import asyncio

from app.database import SessionLocal
from app.services.diagnostics import bot_state_report, format_report


async def _main() -> None:
    async with SessionLocal() as session:
        report = await bot_state_report(session)
    print(format_report(report))


if __name__ == "__main__":
    asyncio.run(_main())

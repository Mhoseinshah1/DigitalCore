"""Background worker entrypoint.

Phase R scaffolding only: a thin async loop that logs a heartbeat roughly every
30 seconds and shuts down cleanly on SIGTERM/SIGINT. No business logic yet.

Run with:  python -m app.worker.main
"""
from __future__ import annotations

import asyncio
import logging
import signal

from app.core.logging import configure_logging

log = logging.getLogger("worker")

HEARTBEAT_SECONDS = 30


async def _run(stop: asyncio.Event) -> None:
    log.info("Worker started (heartbeat every %ss).", HEARTBEAT_SECONDS)
    while not stop.is_set():
        log.info("worker heartbeat")
        try:
            # Wake immediately when a shutdown signal arrives, otherwise tick.
            await asyncio.wait_for(stop.wait(), timeout=HEARTBEAT_SECONDS)
        except asyncio.TimeoutError:
            continue
    log.info("Worker shutting down cleanly.")


async def main() -> None:
    configure_logging()
    stop = asyncio.Event()

    loop = asyncio.get_running_loop()
    for sig in (signal.SIGTERM, signal.SIGINT):
        try:
            loop.add_signal_handler(sig, stop.set)
        except NotImplementedError:  # pragma: no cover - non-Unix fallback
            signal.signal(sig, lambda *_: stop.set())

    await _run(stop)


if __name__ == "__main__":
    try:
        asyncio.run(main())
    except KeyboardInterrupt:  # pragma: no cover - defensive
        pass

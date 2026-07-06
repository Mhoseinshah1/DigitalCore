"""Background worker entrypoint.

A thin async loop that ticks roughly every 30 seconds and shuts down cleanly on
SIGTERM/SIGINT. On each tick it runs the Phase 6 expiry sweep — a DB-only pass
that flips active-but-past-expiry V2Ray services to `expired`. It makes NO panel
calls (so it can never spam a 3X-UI server) and is error-isolated so a failure
never crashes the worker.

TODO(phase 6+): a batched, rate-limited traffic-usage refresh across active
services (calls the panel) belongs here too — left out for now to avoid panel
spam; `v2ray_service.refresh_service_usage` is ready to be scheduled.

Run with:  python -m app.worker.main
"""
from __future__ import annotations

import asyncio
import logging
import signal

from app.core.logging import configure_logging

log = logging.getLogger("worker")

HEARTBEAT_SECONDS = 30


async def _expiry_sweep() -> None:
    """DB-only: mark active-but-expired V2Ray services as expired. Never raises."""
    try:
        from app.database import SessionLocal
        from app.services import v2ray_service
        async with SessionLocal() as session:
            n = await v2ray_service.mark_expired_services(session)
        if n:
            log.info("expiry sweep: marked %s service(s) expired", n)
    except Exception as exc:  # noqa: BLE001 - error isolation: never crash the loop
        log.warning("expiry sweep failed: %s", exc)


async def _run(stop: asyncio.Event) -> None:
    log.info("Worker started (tick every %ss).", HEARTBEAT_SECONDS)
    while not stop.is_set():
        await _expiry_sweep()
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

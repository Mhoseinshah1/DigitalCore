"""Background worker entrypoint.

A thin async loop that ticks roughly every 30 seconds and shuts down cleanly on
SIGTERM/SIGINT. On each tick it runs the V2Ray lifecycle sweep (Phase 8):

  * DB-only marking of expired / over-quota services (cheap, every tick);
  * an interval-gated, batched traffic-usage refresh from the panel
    (``v2ray_usage_refresh_*`` settings) — never spammy: each service is synced
    at most once per interval and only a small batch runs per tick;
  * optional panel auto-disable of expired / over-quota clients
    (``v2ray_auto_disable_*`` settings), idempotent per service;
  * one-shot expiry / traffic warnings to owners.

Every step is error-isolated (``lifecycle_tick`` catches per-step) so a panel
timeout or a bad row never crashes the loop.

Run with:  python -m app.worker.main
"""
from __future__ import annotations

import asyncio
import logging
import signal

from app.core.logging import configure_logging

log = logging.getLogger("worker")

HEARTBEAT_SECONDS = 30


async def _lifecycle_sweep() -> None:
    """Run one V2Ray lifecycle pass. Never raises (each step is isolated)."""
    try:
        from app.database import SessionLocal
        from app.services import v2ray_lifecycle_service
        async with SessionLocal() as session:
            result = await v2ray_lifecycle_service.lifecycle_tick(session)
        acted = {k: v for k, v in result.items() if v}  # keep the log quiet
        if acted:
            log.info("lifecycle sweep: %s", acted)
    except Exception as exc:  # noqa: BLE001 - error isolation: never crash the loop
        log.warning("lifecycle sweep failed: %s", exc)


async def _run(stop: asyncio.Event) -> None:
    log.info("Worker started (tick every %ss).", HEARTBEAT_SECONDS)
    while not stop.is_set():
        await _lifecycle_sweep()
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

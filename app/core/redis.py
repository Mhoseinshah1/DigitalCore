"""Async Redis client.

A single shared client built from settings.REDIS_URL, plus a readiness helper.
Nothing in the app is coupled to Redis yet — this is wiring for later phases.
"""
from __future__ import annotations

import logging
from functools import lru_cache

from redis.asyncio import Redis

from app.config import settings

log = logging.getLogger("redis")


@lru_cache(maxsize=1)
def get_redis() -> Redis:
    """Return the shared async Redis client (created lazily from REDIS_URL)."""
    return Redis.from_url(
        settings.REDIS_URL,
        encoding="utf-8",
        decode_responses=True,
    )


async def redis_ok() -> bool:
    """PING Redis and return whether it responded. Never raises."""
    try:
        return bool(await get_redis().ping())
    except Exception as exc:  # noqa: BLE001 - readiness must never propagate
        # Note: str(exc) for connection errors does not include the URL/password.
        log.warning("Redis ping failed: %s", exc)
        return False

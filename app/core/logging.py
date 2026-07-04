"""Structured logging configuration.

configure_logging() sets a consistent format and a level driven by LOG_LEVEL.
It intentionally configures formatting only and never logs any values, so no
secret (bot token, passwords, Fernet key, JWT secret) is ever emitted here.
"""
from __future__ import annotations

import logging

from app.config import settings

_FORMAT = "%(asctime)s %(levelname)-8s %(name)s: %(message)s"
_DATEFMT = "%Y-%m-%d %H:%M:%S"


def configure_logging(level: str | None = None) -> None:
    """Configure root logging once, using LOG_LEVEL (or an explicit override)."""
    resolved = (level or settings.LOG_LEVEL or "INFO").upper()
    numeric = getattr(logging, resolved, logging.INFO)
    logging.basicConfig(
        level=numeric,
        format=_FORMAT,
        datefmt=_DATEFMT,
        force=True,
    )

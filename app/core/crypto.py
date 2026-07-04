"""Encryption of secret settings at rest, using the boot FERNET_KEY.

Secret-flagged settings (API tokens, panel passwords for third-party servers,
etc.) are stored as Fernet ciphertext so a database dump never leaks them in the
clear. If FERNET_KEY is missing or malformed we deterministically derive a key
from SECRET_KEY so the app still boots in development instead of crashing.
"""
from __future__ import annotations

import base64
import hashlib
import logging
from functools import lru_cache

from cryptography.fernet import Fernet, InvalidToken

from app.config import settings

log = logging.getLogger("crypto")

_PREFIX = "enc::"


def _derive_key_from_secret() -> bytes:
    digest = hashlib.sha256(settings.SECRET_KEY.encode("utf-8")).digest()
    return base64.urlsafe_b64encode(digest)


@lru_cache
def _fernet() -> Fernet:
    key = (settings.FERNET_KEY or "").strip()
    if key:
        try:
            return Fernet(key.encode("utf-8"))
        except (ValueError, TypeError):
            log.warning(
                "FERNET_KEY is set but malformed; falling back to a SECRET_KEY-derived "
                "key. Fix FERNET_KEY (urlsafe base64 of 32 bytes)."
            )
    else:
        log.warning(
            "FERNET_KEY is not set; encrypting secret settings with a key derived from "
            "SECRET_KEY. Set a dedicated FERNET_KEY for production."
        )
    # Fall back to a key derived from SECRET_KEY (development convenience).
    return Fernet(_derive_key_from_secret())


def encrypt(value: str) -> str:
    """Encrypt a plaintext value; returns a marked ciphertext string."""
    if value == "":
        return ""
    token = _fernet().encrypt(value.encode("utf-8")).decode("utf-8")
    return _PREFIX + token


def decrypt(stored: str) -> str:
    """Decrypt a stored value. Tolerates legacy plaintext values."""
    if not stored:
        return ""
    if not stored.startswith(_PREFIX):
        # Value was never encrypted (e.g. seeded plaintext) — return as-is.
        return stored
    token = stored[len(_PREFIX):]
    try:
        return _fernet().decrypt(token.encode("utf-8")).decode("utf-8")
    except InvalidToken:
        # Wrong/rotated key: surface it rather than silently returning empty, which
        # would look like data loss. The caller still gets "" so the panel renders.
        log.warning(
            "Failed to decrypt a secret setting (FERNET_KEY changed or data corrupt?)."
        )
        return ""

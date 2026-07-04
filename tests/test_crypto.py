"""Crypto round-trip — also proves the AttributeError config bug is closed.

Importing app.core.crypto and running encrypt/decrypt requires settings.SECRET_KEY
and settings.FERNET_KEY to exist; before Phase R this raised AttributeError.
"""
from __future__ import annotations

from app.core import crypto


def test_encrypt_decrypt_roundtrip() -> None:
    plaintext = "super-secret-value-123"
    token = crypto.encrypt(plaintext)
    assert token.startswith("enc::")
    assert token != plaintext
    assert crypto.decrypt(token) == plaintext


def test_empty_stays_empty() -> None:
    assert crypto.encrypt("") == ""
    assert crypto.decrypt("") == ""


def test_plaintext_without_prefix_passes_through() -> None:
    # A value that was never encrypted is returned unchanged.
    assert crypto.decrypt("plain-no-prefix") == "plain-no-prefix"

"""Config reconciliation: the crypto/panel keys must be present and defaulted."""
from __future__ import annotations

from app.config import Settings, settings

RECONCILED_KEYS = ("SECRET_KEY", "FERNET_KEY", "BACKUP_ENCRYPTION_KEY", "WEB_PANEL_URL", "LOG_LEVEL")


def test_reconciled_keys_present_on_singleton() -> None:
    for key in RECONCILED_KEYS:
        assert hasattr(settings, key), f"settings is missing {key}"


def test_defaults() -> None:
    s = Settings(_env_file=None)
    assert s.SECRET_KEY == "change_me"
    assert s.FERNET_KEY == ""
    assert s.BACKUP_ENCRYPTION_KEY == ""
    assert s.WEB_PANEL_URL == ""
    assert s.LOG_LEVEL == "INFO"

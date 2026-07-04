"""SettingsService: typed round-trips, validation, encryption, audited changes."""
from __future__ import annotations

import pytest
from sqlalchemy import select

from app.core.defaults import DEFAULTS_BY_KEY, SettingDef
from app.core.settings_service import SECRET_REDACTED, SettingsService
from app.models import AuditLog, Setting

SECRET_KEY_NAME = "test_api_token"


@pytest.fixture
def secret_setting(monkeypatch) -> str:
    """Inject a secret entry into the catalog for the duration of a test."""
    monkeypatch.setitem(
        DEFAULTS_BY_KEY,
        SECRET_KEY_NAME,
        SettingDef(
            SECRET_KEY_NAME,
            "telegram",
            "secret",
            is_secret=True,
            label="Test API token",
        ),
    )
    return SECRET_KEY_NAME


async def test_str_round_trip(db_session) -> None:
    svc = SettingsService(db_session)
    await svc.set("card_number", "6037-0000-1111-2222")
    assert await svc.get_str("card_number") == "6037-0000-1111-2222"
    assert await svc.get_str("missing_key_xyz", "fallback") == "fallback"


async def test_bool_round_trip(db_session) -> None:
    svc = SettingsService(db_session)
    await svc.set("sales_enabled", False)
    assert await svc.get_bool("sales_enabled", True) is False
    await svc.set("sales_enabled", "true")
    assert await svc.get_bool("sales_enabled") is True
    assert await svc.get_bool("missing_key_xyz", True) is True


async def test_int_round_trip(db_session) -> None:
    svc = SettingsService(db_session)
    await svc.set("min_wallet_topup", "25000")
    assert await svc.get_int("min_wallet_topup") == 25000
    assert await svc.get_int("missing_key_xyz", 7) == 7


async def test_validation_rejects_bad_int(db_session) -> None:
    svc = SettingsService(db_session)
    with pytest.raises(ValueError):
        await svc.set("min_wallet_topup", "not-a-number")


async def test_validation_rejects_bad_bool(db_session) -> None:
    svc = SettingsService(db_session)
    with pytest.raises(ValueError):
        await svc.set("sales_enabled", "maybe")


async def test_secret_encrypted_at_rest_and_decrypts(db_session, secret_setting) -> None:
    svc = SettingsService(db_session)
    plaintext = "super-secret-token-123"
    await svc.set(secret_setting, plaintext, actor_type="admin", actor_id=1)

    row = (
        await db_session.execute(select(Setting).where(Setting.key == secret_setting))
    ).scalar_one()
    assert row.is_secret is True
    assert row.value != plaintext
    assert plaintext not in row.value
    assert row.value.startswith("enc::")

    assert await svc.get_str(secret_setting) == plaintext


async def test_secret_audit_entry_is_redacted(db_session, secret_setting) -> None:
    svc = SettingsService(db_session)
    plaintext = "another-secret-456"
    await svc.set(secret_setting, plaintext, actor_type="admin", actor_id=1)

    audit = (
        await db_session.execute(
            select(AuditLog).where(AuditLog.target_id == secret_setting)
        )
    ).scalars().one()
    assert audit.new_value == SECRET_REDACTED
    assert plaintext not in (audit.new_value or "")
    assert plaintext not in (audit.old_value or "")


async def test_change_writes_audit_row_with_old_and_new(db_session) -> None:
    svc = SettingsService(db_session)
    await svc.set("payment_text", "old text", actor_type="admin", actor_id=9)
    await svc.set("payment_text", "new text", actor_type="admin", actor_id=9)

    audits = (
        (
            await db_session.execute(
                select(AuditLog)
                .where(AuditLog.target_id == "payment_text")
                .order_by(AuditLog.id)
            )
        )
        .scalars()
        .all()
    )
    assert len(audits) == 2
    assert audits[0].old_value == "" and audits[0].new_value == "old text"
    assert audits[1].old_value == "old text" and audits[1].new_value == "new text"
    assert audits[1].actor_type == "admin" and audits[1].actor_id == 9
    assert audits[1].action == "setting.changed"


async def test_no_audit_when_value_unchanged(db_session) -> None:
    svc = SettingsService(db_session)
    await svc.set("start_text", "hello")
    await svc.set("start_text", "hello")  # identical — no second audit row

    audits = (
        (
            await db_session.execute(
                select(AuditLog).where(AuditLog.target_id == "start_text")
            )
        )
        .scalars()
        .all()
    )
    assert len(audits) == 1


async def test_seed_style_set_skips_audit(db_session) -> None:
    svc = SettingsService(db_session)
    await svc.set("rules_text", "seeded", audit=False)
    audits = (
        (
            await db_session.execute(
                select(AuditLog).where(AuditLog.target_id == "rules_text")
            )
        )
        .scalars()
        .all()
    )
    assert audits == []
    assert await svc.get_str("rules_text") == "seeded"

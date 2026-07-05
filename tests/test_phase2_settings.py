"""Phase 2 settings: seeding, typed getters (incl. decimal), catalog coverage."""
from __future__ import annotations

from decimal import Decimal

from app.core.defaults import DEFAULTS_BY_KEY
from app.core.settings_service import SettingsService
from app.seed import seed_default_settings

REQUIRED_KEYS = {
    # general
    "site_name", "maintenance_mode", "sales_enabled", "support_enabled",
    # telegram
    "log_group_id", "force_join_channel", "support_username",
    # payment
    "card_number", "card_owner", "sheba_number", "payment_instructions",
    "min_wallet_topup", "wallet_enabled", "card_to_card_enabled",
    # bot texts
    "start_text", "rules_text", "blocked_user_text", "maintenance_text",
    "payment_text", "successful_purchase_text", "rejected_payment_text", "support_text",
}


def test_catalog_has_all_required_keys() -> None:
    assert REQUIRED_KEYS <= set(DEFAULTS_BY_KEY)


async def test_seed_creates_missing_and_is_idempotent(db_session) -> None:
    created = await seed_default_settings(db_session)
    await db_session.commit()
    assert created == len(DEFAULTS_BY_KEY)
    svc = SettingsService(db_session)
    assert await svc.get_str("site_name") == "DigitalCore"
    assert await svc.get_bool("sales_enabled") is True
    # Re-seed does nothing.
    again = await seed_default_settings(db_session)
    await db_session.commit()
    assert again == 0


async def test_seed_never_overwrites_custom_value(db_session) -> None:
    svc = SettingsService(db_session)
    await svc.set("site_name", "CustomStore")
    created = await seed_default_settings(db_session)
    await db_session.commit()
    # Every key EXCEPT the one we set is created; the custom value survives.
    assert created == len(DEFAULTS_BY_KEY) - 1
    assert await svc.get_str("site_name") == "CustomStore"


async def test_typed_getters_including_decimal(db_session) -> None:
    svc = SettingsService(db_session)
    await svc.set("min_wallet_topup", "15000")
    assert await svc.get_int("min_wallet_topup") == 15000
    assert await svc.get_decimal("min_wallet_topup") == Decimal("15000")
    assert await svc.get_decimal("missing_key", Decimal("2.5")) == Decimal("2.5")
    await svc.set("wallet_enabled", False)
    assert await svc.get_bool("wallet_enabled") is False

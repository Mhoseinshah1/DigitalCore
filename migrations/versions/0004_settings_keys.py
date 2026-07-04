"""settings: rename default_card_number/default_sheba/default_card_owner keys

Data-only migration. The settings catalog canonicalises the payment keys to
card_number / sheba / card_owner; existing rows are renamed so operator-entered
values survive the upgrade. No schema change.

Revision ID: 0004_settings_keys
Revises: 0003_phase1_rbac
Create Date: 2025-01-04 00:00:00
"""
from alembic import op

revision = "0004_settings_keys"
down_revision = "0003_phase1_rbac"
branch_labels = None
depends_on = None

_RENAMES = (
    ("default_card_number", "card_number"),
    ("default_sheba", "sheba"),
    ("default_card_owner", "card_owner"),
)


def upgrade() -> None:
    for old, new in _RENAMES:
        # Rename only when the canonical key does not already exist.
        op.execute(
            f"UPDATE settings SET key = '{new}' "
            f"WHERE key = '{old}' "
            f"AND NOT EXISTS (SELECT 1 FROM settings s2 WHERE s2.key = '{new}')"
        )
        # Drop a leftover old row if both somehow exist (canonical row wins).
        op.execute(f"DELETE FROM settings WHERE key = '{old}'")


def downgrade() -> None:
    for old, new in _RENAMES:
        op.execute(
            f"UPDATE settings SET key = '{old}' "
            f"WHERE key = '{new}' "
            f"AND NOT EXISTS (SELECT 1 FROM settings s2 WHERE s2.key = '{old}')"
        )
        op.execute(f"DELETE FROM settings WHERE key = '{new}'")

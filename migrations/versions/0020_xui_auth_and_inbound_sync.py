"""3x-ui refactor: server auth mode / TLS / timeout + inbound sync fields

Additive columns only — existing rows keep working:
  xui_servers:  auth_mode (default 'password'), tls_verify (default 1),
                timeout_seconds (default 20), xray_version (nullable)
  xui_inbounds: tag, enable_from_panel (default 1), raw_json, synced_at

Existing servers default to auth_mode='password' (their username/password keep
working); an admin can switch to an API token in the panel form. Runs on a fresh
and an existing database. batch_alter_table keeps the adds SQLite-safe.

Revision ID: 0020_xui_auth_and_inbound_sync
Revises: 0019_product_categories
Create Date: 2025-04-07 00:00:00
"""
from alembic import op
import sqlalchemy as sa

revision = "0020_xui_auth_and_inbound_sync"
down_revision = "0019_product_categories"
branch_labels = None
depends_on = None


def upgrade() -> None:
    with op.batch_alter_table("xui_servers") as batch:
        batch.add_column(sa.Column("auth_mode", sa.String(length=16),
                                   nullable=False, server_default="password"))
        batch.add_column(sa.Column("tls_verify", sa.Boolean(),
                                   nullable=False, server_default="1"))
        batch.add_column(sa.Column("timeout_seconds", sa.Integer(),
                                   nullable=False, server_default="20"))
        batch.add_column(sa.Column("xray_version", sa.String(length=32), nullable=True))

    with op.batch_alter_table("xui_inbounds") as batch:
        batch.add_column(sa.Column("tag", sa.String(length=255), nullable=True))
        batch.add_column(sa.Column("enable_from_panel", sa.Boolean(),
                                   nullable=False, server_default="1"))
        batch.add_column(sa.Column("raw_json", sa.Text(), nullable=True))
        batch.add_column(sa.Column("synced_at", sa.DateTime(timezone=True), nullable=True))


def downgrade() -> None:
    with op.batch_alter_table("xui_inbounds") as batch:
        batch.drop_column("synced_at")
        batch.drop_column("raw_json")
        batch.drop_column("enable_from_panel")
        batch.drop_column("tag")

    with op.batch_alter_table("xui_servers") as batch:
        batch.drop_column("xray_version")
        batch.drop_column("timeout_seconds")
        batch.drop_column("tls_verify")
        batch.drop_column("auth_mode")

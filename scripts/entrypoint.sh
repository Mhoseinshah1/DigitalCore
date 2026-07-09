#!/usr/bin/env bash
# Container entrypoint. Selects the service role.
#
# Migrations are intentionally NOT run automatically — Phase 1 runs them
# explicitly (docker compose exec backend alembic upgrade head) so the flow is
# predictable. The `migrate` role is available for convenience/automation.
set -euo pipefail

ROLE="${1:-backend}"

# --- Storage bootstrap + privilege drop -----------------------------------
# The compose bind-mount (./storage -> /srv/digitalcore/storage) is owned by the
# HOST user and overlays the image's build-time chown, so the unprivileged `app`
# user ends up unable to create storage/receipts/YYYY/MM — every receipt upload
# then fails with PermissionError ("امکان ذخیره رسید نبود"). Running as root
# here we create the runtime subdirectories and hand the whole tree to `app`,
# then re-exec ourselves via gosu so the service itself never runs as root.
STORAGE_DIR="${STORAGE_ROOT:-/srv/digitalcore/storage}"
if [ "$(id -u)" = "0" ]; then
    mkdir -p \
        "$STORAGE_DIR/receipts" "$STORAGE_DIR/receipts/wallet" \
        "$STORAGE_DIR/exports" "$STORAGE_DIR/logs" \
        "$STORAGE_DIR/backups" "$STORAGE_DIR/temp" 2>/dev/null || true
    # Best-effort: a read-only or unusual mount must not block startup — the app
    # still surfaces a safe error if the path turns out to be unwritable.
    chown -R app:app "$STORAGE_DIR" 2>/dev/null || true
    if command -v gosu >/dev/null 2>&1; then
        exec gosu app "$0" "$@"
    fi
fi

wait_for_db() {
    echo "==> Waiting for the database…"
    python -m app.wait_for_db
}

case "$ROLE" in
    backend|web)
        wait_for_db
        # Opt-in auto-migration: set AUTO_MIGRATE=true in the environment so a
        # plain `docker compose up -d --build backend` also applies pending
        # migrations. Default off keeps the historical explicit-migrate flow.
        if [ "${AUTO_MIGRATE:-false}" = "true" ]; then
            echo "==> AUTO_MIGRATE=true — applying database migrations…"
            alembic upgrade head
        fi
        echo "==> Starting backend API on ${API_HOST:-0.0.0.0}:${API_PORT:-8000}…"
        exec uvicorn app.web.main:app \
            --host "${API_HOST:-0.0.0.0}" \
            --port "${API_PORT:-8000}" \
            --proxy-headers --forwarded-allow-ips='*'
        ;;
    bot)
        echo "==> Starting Telegram bot…"
        exec python -m app.bot.main
        ;;
    worker)
        echo "==> Starting background worker…"
        exec python -m app.worker.main
        ;;
    migrate)
        wait_for_db
        exec alembic upgrade head
        ;;
    create-admin)
        wait_for_db
        exec python scripts/create_admin.py "${@:2}"
        ;;
    shell)
        exec /bin/bash
        ;;
    *)
        echo "Unknown service role: $ROLE" >&2
        echo "Valid roles: backend | bot | worker | migrate | create-admin | shell" >&2
        exit 64
        ;;
esac

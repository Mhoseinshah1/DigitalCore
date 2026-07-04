#!/usr/bin/env bash
# Container entrypoint. Selects the service role.
#
# Migrations are intentionally NOT run automatically — Phase 1 runs them
# explicitly (docker compose exec backend alembic upgrade head) so the flow is
# predictable. The `migrate` role is available for convenience/automation.
set -euo pipefail

ROLE="${1:-backend}"

wait_for_db() {
    echo "==> Waiting for the database…"
    python -m app.wait_for_db
}

case "$ROLE" in
    backend|web)
        wait_for_db
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

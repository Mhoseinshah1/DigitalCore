#!/usr/bin/env bash
# Container entrypoint. Selects the service role and, for the web role, runs
# database migrations and seeds the owner admin + default (empty) business
# settings before starting. Seeding is idempotent, so restarts are safe.
set -euo pipefail

ROLE="${1:-web}"

wait_for_db() {
    echo "==> Waiting for the database…"
    python -m app.wait_for_db
}

case "$ROLE" in
    web)
        wait_for_db
        echo "==> Running database migrations…"
        alembic upgrade head
        echo "==> Seeding owner admin and default settings…"
        python -m app.seed
        echo "==> Starting web panel on :8000…"
        exec uvicorn app.web.main:app --host 0.0.0.0 --port 8000 --proxy-headers --forwarded-allow-ips='*'
        ;;
    bot)
        wait_for_db
        # The bot waits for the web role to have applied migrations/seed (compose
        # gates it on web being healthy), so it does not migrate or seed itself.
        echo "==> Starting Telegram bot…"
        exec python -m app.bot.main
        ;;
    migrate)
        wait_for_db
        exec alembic upgrade head
        ;;
    seed)
        wait_for_db
        exec python -m app.seed
        ;;
    shell)
        exec /bin/bash
        ;;
    *)
        echo "Unknown service role: $ROLE" >&2
        echo "Valid roles: web | bot | migrate | seed | shell" >&2
        exit 64
        ;;
esac

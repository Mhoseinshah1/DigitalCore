#!/usr/bin/env bash
# =============================================================================
# DigitalCore management helper
# =============================================================================
# Usage:
#   bash scripts/manage.sh status
#   bash scripts/manage.sh logs backend
#   bash scripts/manage.sh logs bot
#   bash scripts/manage.sh logs backend --follow
#   bash scripts/manage.sh restart
#   bash scripts/manage.sh down
#   bash scripts/manage.sh health
#   bash scripts/manage.sh backup
#   bash scripts/manage.sh restore --latest --yes
#   bash scripts/manage.sh update
#
# Logs do NOT follow by default; add --follow (or -f) to stream.
# =============================================================================
set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
ROOT_DIR="$(cd "$SCRIPT_DIR/.." && pwd)"
cd "$ROOT_DIR"

if docker compose version >/dev/null 2>&1; then
    COMPOSE="docker compose"
elif command -v docker-compose >/dev/null 2>&1; then
    COMPOSE="docker-compose"
else
    echo "Docker Compose is not installed." >&2
    exit 1
fi

API_PORT="$(grep -E '^API_PORT=' .env 2>/dev/null | cut -d= -f2 || true)"
API_PORT="${API_PORT:-8000}"

usage() {
    sed -n '2,23p' "$0"
    exit "${1:-0}"
}

cmd="${1:-}"
case "$cmd" in
    status)
        $COMPOSE ps
        ;;
    logs)
        service="${2:-}"
        [ -n "$service" ] || { echo "Usage: manage.sh logs <service> [--follow]" >&2; exit 2; }
        follow=""
        case "${3:-}" in
            --follow|-f) follow="-f" ;;
        esac
        if [ -n "$follow" ]; then
            $COMPOSE logs "$follow" --tail=200 "$service"
        else
            $COMPOSE logs --tail=200 "$service"
        fi
        ;;
    restart)
        $COMPOSE restart
        ;;
    down)
        $COMPOSE down
        ;;
    health)
        exec bash "$SCRIPT_DIR/healthcheck.sh"
        ;;
    backup)
        exec bash "$SCRIPT_DIR/backup.sh" "${@:2}"
        ;;
    restore)
        exec bash "$SCRIPT_DIR/restore.sh" "${@:2}"
        ;;
    update)
        exec bash "$SCRIPT_DIR/update.sh" "${@:2}"
        ;;
    ""|-h|--help|help)
        usage 0
        ;;
    *)
        echo "Unknown command: $cmd" >&2
        usage 2
        ;;
esac

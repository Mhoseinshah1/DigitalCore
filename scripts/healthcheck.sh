#!/usr/bin/env bash
# =============================================================================
# DigitalCore — health check
# =============================================================================
# Shows compose status and verifies that postgres, redis and backend are running
# and that /health and /ready both return 200. Exits 0 if everything is healthy,
# non-zero otherwise. Used by update.sh and for manual checks.
# =============================================================================
set -euo pipefail

if [ -t 1 ]; then
    BOLD=$'\033[1m'; RED=$'\033[31m'; GREEN=$'\033[32m'; YELLOW=$'\033[33m'; CYAN=$'\033[36m'; RESET=$'\033[0m'
else
    BOLD=""; RED=""; GREEN=""; YELLOW=""; CYAN=""; RESET=""
fi
info() { printf '%s\n' "${CYAN}==>${RESET} $*"; }
ok()   { printf '%s\n' "${GREEN}✓${RESET} $*"; }
warn() { printf '%s\n' "${YELLOW}!${RESET} $*"; }
die()  { printf '%s\n' "${RED}✗ $*${RESET}" >&2; exit 1; }

ROOT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
cd "$ROOT_DIR"
ENV_FILE="$ROOT_DIR/.env"

env_get() {
    local key="$1" default="${2:-}" line val
    [ -f "$ENV_FILE" ] || { printf '%s' "$default"; return; }
    line="$(grep -E "^${key}=" "$ENV_FILE" | head -n1 || true)"
    [ -n "$line" ] || { printf '%s' "$default"; return; }
    val="${line#*=}"
    val="${val%$'\r'}"
    printf '%s' "$val"
}

COMPOSE=()
if docker compose version >/dev/null 2>&1; then
    COMPOSE=(docker compose)
elif command -v docker-compose >/dev/null 2>&1; then
    COMPOSE=(docker-compose)
else
    die "Docker Compose is not installed."
fi

API_PORT="$(env_get API_PORT 8000)"
HEALTH_URL="http://localhost:${API_PORT}/health"
READY_URL="http://localhost:${API_PORT}/ready"

failures=0

# --- container status --------------------------------------------------------
info "Compose service status:"
"${COMPOSE[@]}" ps || true

container_running() {
    # container_running SERVICE -> 0 if the service's container is running
    local service="$1" cid state
    cid="$("${COMPOSE[@]}" ps -q "$service" 2>/dev/null || true)"
    [ -n "$cid" ] || return 1
    state="$(docker inspect -f '{{.State.Running}}' "$cid" 2>/dev/null || echo false)"
    [ "$state" = "true" ]
}

for service in postgres redis backend; do
    if container_running "$service"; then
        ok "container '${service}' is running."
    else
        warn "container '${service}' is NOT running."
        failures=$((failures + 1))
    fi
done

# --- HTTP endpoints ----------------------------------------------------------
check_http() {
    # check_http NAME URL
    local name="$1" url="$2" code
    code="$(curl -s -o /dev/null -w '%{http_code}' --max-time 5 "$url" 2>/dev/null || true)"
    if [ "$code" = "200" ]; then
        ok "${name} returned 200."
    else
        warn "${name} returned '${code:-no response}' (expected 200)."
        failures=$((failures + 1))
    fi
}
check_http "/health" "$HEALTH_URL"
check_http "/ready" "$READY_URL"

# --- verdict -----------------------------------------------------------------
if [ "$failures" -eq 0 ]; then
    printf '%s\n' "${GREEN}${BOLD}All healthy.${RESET}"
    exit 0
fi
printf '%s\n' "${RED}${BOLD}${failures} health check(s) failed.${RESET}" >&2
exit 1

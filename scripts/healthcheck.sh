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

# --- database connectivity ---------------------------------------------------
PGUSER="$(env_get POSTGRES_USER digitalcore)"
PGDB="$(env_get POSTGRES_DB digitalcore)"
if container_running postgres; then
    if "${COMPOSE[@]}" exec -T postgres pg_isready -U "$PGUSER" -d "$PGDB" >/dev/null 2>&1; then
        ok "PostgreSQL accepts connections."
    else
        warn "PostgreSQL is not accepting connections."
        failures=$((failures + 1))
    fi
fi

# --- redis connectivity ------------------------------------------------------
if container_running redis; then
    if [ "$("${COMPOSE[@]}" exec -T redis redis-cli ping 2>/dev/null | tr -d '\r')" = "PONG" ]; then
        ok "Redis responds to PING."
    else
        warn "Redis did not respond to PING."
        failures=$((failures + 1))
    fi
fi

# --- disk usage --------------------------------------------------------------
info "Disk usage (repo filesystem):"
df -h "$ROOT_DIR" | tail -n +1 >&2 || true
BACKUP_DIR="$ROOT_DIR/storage/backups"
if [ -d "$BACKUP_DIR" ]; then
    bcount="$(find "$BACKUP_DIR" -type f \( -name '*.tar.gz' -o -name '*.sql.gz' -o -name '*.tar.gz.enc' \) 2>/dev/null | wc -l | tr -d ' ')"
    bsize="$(du -sh "$BACKUP_DIR" 2>/dev/null | cut -f1)"
    ok "Backups: ${bcount} file(s), ${bsize:-0} under storage/backups."
fi

# --- recent errors in service logs (last 200 lines) --------------------------
for service in backend bot worker; do
    if container_running "$service"; then
        n="$("${COMPOSE[@]}" logs --tail 200 "$service" 2>/dev/null | grep -ciE '\b(error|traceback|critical)\b' || true)"
        if [ "${n:-0}" -gt 0 ]; then
            warn "${service}: ${n} error-like line(s) in the last 200 log lines."
        else
            ok "${service}: no recent errors in the last 200 log lines."
        fi
    fi
done

# --- verdict -----------------------------------------------------------------
if [ "$failures" -eq 0 ]; then
    printf '%s\n' "${GREEN}${BOLD}All healthy.${RESET}"
    exit 0
fi
printf '%s\n' "${RED}${BOLD}${failures} health check(s) failed.${RESET}" >&2
exit 1

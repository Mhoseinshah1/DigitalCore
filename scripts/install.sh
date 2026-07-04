#!/usr/bin/env bash
# =============================================================================
# DigitalCore installer v0 (Phase 1)
# =============================================================================
# Brings up the stack in development/staging and verifies it is actually healthy.
# It does NOT claim success unless /health and /ready both pass.
#
# Steps:
#   - check Ubuntu, Docker, Docker Compose
#   - create .env from .env.example if missing, prompting for key values
#   - docker compose up -d --build
#   - alembic upgrade head
#   - create the super admin
#   - verify /health and /ready
# =============================================================================
set -euo pipefail

if [ -t 1 ]; then
    BOLD="$(printf '\033[1m')"; RED="$(printf '\033[31m')"; GREEN="$(printf '\033[32m')"
    YELLOW="$(printf '\033[33m')"; CYAN="$(printf '\033[36m')"; RESET="$(printf '\033[0m')"
else
    BOLD=""; RED=""; GREEN=""; YELLOW=""; CYAN=""; RESET=""
fi
info() { printf '%s\n' "${CYAN}==>${RESET} $*"; }
ok()   { printf '%s\n' "${GREEN}✓${RESET} $*"; }
warn() { printf '%s\n' "${YELLOW}!${RESET} $*"; }
die()  { printf '%s\n' "${RED}✗ $*${RESET}" >&2; exit 1; }

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
ROOT_DIR="$(cd "$SCRIPT_DIR/.." && pwd)"
cd "$ROOT_DIR"

API_PORT_DEFAULT=8000

# --- 1. OS / dependency checks ----------------------------------------------
info "Checking the operating system…"
if [ -r /etc/os-release ]; then
    . /etc/os-release
    if [ "${ID:-}" = "ubuntu" ]; then
        ok "Ubuntu ${VERSION_ID:-} detected."
    else
        warn "This installer targets Ubuntu; detected '${ID:-unknown}'. Continuing anyway."
    fi
else
    warn "Could not detect the OS (no /etc/os-release). Continuing anyway."
fi

info "Checking Docker…"
command -v docker >/dev/null 2>&1 || die "Docker is not installed. See https://docs.docker.com/engine/install/ubuntu/"
docker info >/dev/null 2>&1 || die "The Docker daemon is not running or you lack permission (try with sudo)."
ok "Docker is available."

info "Checking Docker Compose…"
if docker compose version >/dev/null 2>&1; then
    COMPOSE="docker compose"
elif command -v docker-compose >/dev/null 2>&1; then
    COMPOSE="docker-compose"
else
    die "Docker Compose is not installed. See https://docs.docker.com/compose/install/"
fi
ok "Docker Compose is available."

# --- 2. .env ----------------------------------------------------------------
prompt() {
    # prompt VAR "Question" "default" [secret]
    local __var="$1" __q="$2" __def="${3:-}" __secret="${4:-}" __val=""
    if [ -n "$__secret" ]; then
        printf '%s' "${BOLD}$__q${RESET} "
        IFS= read -rs __val || true; printf '\n'
    else
        if [ -n "$__def" ]; then
            printf '%s' "${BOLD}$__q${RESET} [${__def}] "
        else
            printf '%s' "${BOLD}$__q${RESET} "
        fi
        IFS= read -r __val || true
    fi
    [ -z "$__val" ] && __val="$__def"
    printf -v "$__var" '%s' "$__val"
}

# set_env KEY VALUE — replace or append KEY=VALUE in .env (value taken literally)
set_env() {
    local key="$1" val="$2"
    if grep -qE "^${key}=" .env; then
        # Use a temp file to avoid sed delimiter issues with slashes in values.
        awk -v k="$key" -v v="$val" 'BEGIN{FS=OFS="="}
            $1==k {print k"="v; next} {print}' .env > .env.tmp && mv .env.tmp .env
    else
        printf '%s=%s\n' "$key" "$val" >> .env
    fi
}

if [ -f .env ]; then
    ok ".env already exists — keeping it (edit it by hand to change values)."
else
    [ -f .env.example ] || die ".env.example is missing; cannot create .env."
    cp .env.example .env
    ok "Created .env from .env.example."

    printf '\n%s\n' "${BOLD}A few values are needed (press Enter to accept defaults):${RESET}"
    prompt DOMAIN        "Domain (optional):" ""
    prompt ADMIN_EMAIL   "Admin email:" "admin@example.com"
    prompt ADMIN_PASSWORD "Admin password:" "" secret
    while [ -z "${ADMIN_PASSWORD}" ]; do
        warn "Admin password cannot be empty."
        prompt ADMIN_PASSWORD "Admin password:" "" secret
    done
    prompt TELEGRAM_BOT_TOKEN "Telegram bot token (optional):" ""
    prompt TELEGRAM_ADMIN_ID  "Telegram admin id (optional):" ""

    set_env ADMIN_EMAIL "$ADMIN_EMAIL"
    set_env ADMIN_PASSWORD "$ADMIN_PASSWORD"
    set_env TELEGRAM_BOT_TOKEN "$TELEGRAM_BOT_TOKEN"
    set_env TELEGRAM_ADMIN_ID "$TELEGRAM_ADMIN_ID"
    [ -n "${DOMAIN:-}" ] && set_env DOMAIN "$DOMAIN"
    ok "Wrote configuration to .env"
fi

# Figure out which host port the backend is published on (defaults to 8000).
API_PORT="$(grep -E '^API_PORT=' .env | cut -d= -f2 || true)"
API_PORT="${API_PORT:-$API_PORT_DEFAULT}"
HEALTH_URL="http://localhost:${API_PORT}/health"
READY_URL="http://localhost:${API_PORT}/ready"

# --- 3. Build & start -------------------------------------------------------
info "Building and starting the stack…"
$COMPOSE up -d --build

# --- 4. Migrate -------------------------------------------------------------
info "Applying database migrations…"
$COMPOSE exec -T backend alembic upgrade head

# --- 5. Create admin --------------------------------------------------------
info "Creating the super admin…"
$COMPOSE exec -T backend python scripts/create_admin.py

# --- 6. Health / readiness gate ---------------------------------------------
dump_diagnostics() {
    warn "Dumping diagnostics:"
    echo "----- docker ps -a -----"
    docker ps -a || true
    echo "----- docker compose logs backend --tail=200 -----"
    $COMPOSE logs backend --tail=200 || true
}

info "Waiting for the backend to become healthy…"
health_ok=""
for _ in $(seq 1 30); do
    if curl -fsS "$HEALTH_URL" >/dev/null 2>&1; then health_ok="yes"; break; fi
    sleep 2
done
if [ -z "$health_ok" ]; then
    dump_diagnostics
    die "Backend /health did not pass. Installation FAILED."
fi
HEALTH_BODY="$(curl -fsS "$HEALTH_URL")"
ok "/health passed: $HEALTH_BODY"

info "Checking readiness (database)…"
ready_ok=""
for _ in $(seq 1 30); do
    if curl -fsS "$READY_URL" >/dev/null 2>&1; then ready_ok="yes"; break; fi
    sleep 2
done
if [ -z "$ready_ok" ]; then
    dump_diagnostics
    die "Backend /ready did not pass (database not ready). Installation FAILED."
fi
READY_BODY="$(curl -fsS "$READY_URL")"
ok "/ready passed: $READY_BODY"

printf '\n%s\n' "${GREEN}${BOLD}DigitalCore is installed and healthy.${RESET}"
printf '  API:    http://localhost:%s\n' "$API_PORT"
printf '  Health: %s\n' "$HEALTH_URL"
printf '  Ready:  %s\n' "$READY_URL"
printf '  Manage: bash scripts/manage.sh status | logs backend | health\n\n'

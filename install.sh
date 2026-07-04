#!/usr/bin/env bash
# =============================================================================
# DigitalCore — one-command installer
# =============================================================================
#
# Design rule: the installer is MINIMAL. It asks only what is required to boot
# the platform and nothing about the business (cards, channels, plans, texts...).
# Everything else is configured later from the admin web panel or the Telegram
# admin panel.
#
# The installer asks only for:
#   1. Telegram BOT_TOKEN            (required)
#   2. Main admin Telegram ID        (required)
#   3. Web panel domain              (required)
#   4. Web admin password            (optional – generated if left blank)
#
# It generates every secret automatically, brings the stack up with Docker
# Compose, seeds the owner admin + empty business-settings records, and prints
# the panel URL and credentials once at the end.
#
# Usage:
#   ./install.sh                 interactive install
#   ./install.sh --non-interactive   read answers from environment variables
#
# Non-interactive environment variables (all optional except the first three):
#   BOT_TOKEN, MAIN_ADMIN_TELEGRAM_ID, WEB_PANEL_DOMAIN, WEB_ADMIN_PASSWORD
# =============================================================================

set -euo pipefail

# --- pretty output ----------------------------------------------------------
if [ -t 1 ]; then
    BOLD="$(printf '\033[1m')"; DIM="$(printf '\033[2m')"; RED="$(printf '\033[31m')"
    GREEN="$(printf '\033[32m')"; YELLOW="$(printf '\033[33m')"; CYAN="$(printf '\033[36m')"
    RESET="$(printf '\033[0m')"
else
    BOLD=""; DIM=""; RED=""; GREEN=""; YELLOW=""; CYAN=""; RESET=""
fi

info()  { printf '%s\n' "${CYAN}==>${RESET} $*"; }
ok()    { printf '%s\n' "${GREEN}✓${RESET} $*"; }
warn()  { printf '%s\n' "${YELLOW}!${RESET} $*"; }
err()   { printf '%s\n' "${RED}✗ $*${RESET}" >&2; }
die()   { err "$*"; exit 1; }

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
cd "$SCRIPT_DIR"

ENV_FILE="$SCRIPT_DIR/.env"
NON_INTERACTIVE=0
[ "${1:-}" = "--non-interactive" ] && NON_INTERACTIVE=1

# --- banner -----------------------------------------------------------------
printf '%s\n' "${BOLD}"
cat <<'BANNER'
  ____  _       _ _        _  ____
 |  _ \(_) __ _(_) |_ __ _| |/ ___|___  _ __ ___
 | | | | |/ _` | | __/ _` | | |   / _ \| '__/ _ \
 | |_| | | (_| | | || (_| | | |__| (_) | | |  __/
 |____/|_|\__, |_|\__\__,_|_|\____\___/|_|  \___|
          |___/           one-command installer
BANNER
printf '%s\n' "${RESET}"

# --- dependency checks ------------------------------------------------------
info "Checking dependencies…"
command -v docker >/dev/null 2>&1 || die "Docker is not installed. See https://docs.docker.com/engine/install/"
if docker compose version >/dev/null 2>&1; then
    COMPOSE="docker compose"
elif command -v docker-compose >/dev/null 2>&1; then
    COMPOSE="docker-compose"
else
    die "Docker Compose is not installed. See https://docs.docker.com/compose/install/"
fi
docker info >/dev/null 2>&1 || die "The Docker daemon is not running or you lack permission to use it."
ok "Docker and Docker Compose are available."

# --- secret generation helpers ----------------------------------------------
# hex secret of N bytes
gen_hex() {
    local bytes="${1:-32}"
    if command -v openssl >/dev/null 2>&1; then
        openssl rand -hex "$bytes"
    else
        head -c "$bytes" /dev/urandom | od -An -tx1 | tr -d ' \n'
    fi
}

# urlsafe base64 of 32 random bytes → valid Fernet key
gen_fernet() {
    if command -v openssl >/dev/null 2>&1; then
        openssl rand -base64 32 | tr '+/' '-_'
    else
        head -c 32 /dev/urandom | base64 | tr '+/' '-_'
    fi
}

# alphanumeric password, ~24 chars, safe to copy/paste
gen_password() {
    if command -v openssl >/dev/null 2>&1; then
        openssl rand -base64 32 | tr -dc 'A-Za-z0-9' | head -c 24
    else
        head -c 48 /dev/urandom | base64 | tr -dc 'A-Za-z0-9' | head -c 24
    fi
}

# --- prompt helpers ---------------------------------------------------------
prompt_required() {
    # prompt_required VAR_NAME "Question" [env_default]
    local __var="$1" __q="$2" __envdefault="${3:-}" __val=""
    if [ "$NON_INTERACTIVE" = "1" ]; then
        __val="$__envdefault"
        [ -n "$__val" ] || die "Missing required value for $__var in non-interactive mode."
        printf -v "$__var" '%s' "$__val"
        return
    fi
    while :; do
        printf '%s' "${BOLD}$__q${RESET} "
        IFS= read -r __val || true
        __val="${__val#"${__val%%[![:space:]]*}"}"   # ltrim
        __val="${__val%"${__val##*[![:space:]]}"}"   # rtrim
        if [ -n "$__val" ]; then
            printf -v "$__var" '%s' "$__val"
            return
        fi
        warn "This value is required."
    done
}

prompt_secret_optional() {
    # prompt_secret_optional VAR_NAME "Question" [env_default]
    local __var="$1" __q="$2" __envdefault="${3:-}" __val=""
    if [ "$NON_INTERACTIVE" = "1" ]; then
        printf -v "$__var" '%s' "$__envdefault"
        return
    fi
    printf '%s' "${BOLD}$__q${RESET} "
    IFS= read -rs __val || true
    printf '\n'
    printf -v "$__var" '%s' "$__val"
}

validate_telegram_id() {
    case "$1" in
        ''|*[!0-9]*) return 1 ;;
        *) return 0 ;;
    esac
}

# --- refuse to clobber an existing install ----------------------------------
if [ -f "$ENV_FILE" ]; then
    warn "An existing .env was found at $ENV_FILE"
    if [ "$NON_INTERACTIVE" = "1" ]; then
        die "Refusing to overwrite an existing .env in non-interactive mode. Remove it first."
    fi
    printf '%s' "${BOLD}Overwrite it and re-run setup? [y/N]${RESET} "
    IFS= read -r _ans || true
    case "${_ans:-}" in
        y|Y|yes|YES) info "Continuing; a backup will be written to .env.bak" ; cp "$ENV_FILE" "$ENV_FILE.bak" ;;
        *) die "Aborted. Nothing was changed." ;;
    esac
fi

# =============================================================================
# The ONLY four questions the installer is allowed to ask.
# =============================================================================
printf '\n%s\n' "${BOLD}The installer only needs four things to boot the platform:${RESET}"
printf '%s\n\n' "${DIM}(Cards, channels, plans and all texts are configured later in the panel.)${RESET}"

# 1. BOT_TOKEN
prompt_required BOT_TOKEN "1/4  Telegram BOT_TOKEN:" "${BOT_TOKEN:-}"

# 2. MAIN_ADMIN_TELEGRAM_ID
while :; do
    prompt_required MAIN_ADMIN_TELEGRAM_ID "2/4  Main admin Telegram numeric ID:" "${MAIN_ADMIN_TELEGRAM_ID:-}"
    if validate_telegram_id "$MAIN_ADMIN_TELEGRAM_ID"; then break; fi
    [ "$NON_INTERACTIVE" = "1" ] && die "MAIN_ADMIN_TELEGRAM_ID must be numeric."
    warn "The Telegram ID must be a number (e.g. 123456789)."
done

# 3. WEB_PANEL_DOMAIN
prompt_required WEB_PANEL_DOMAIN "3/4  Web panel domain (e.g. panel.example.com):" "${WEB_PANEL_DOMAIN:-}"
# strip scheme and trailing slash if the user pasted a URL
WEB_PANEL_DOMAIN="${WEB_PANEL_DOMAIN#http://}"
WEB_PANEL_DOMAIN="${WEB_PANEL_DOMAIN#https://}"
WEB_PANEL_DOMAIN="${WEB_PANEL_DOMAIN%%/*}"

# 4. WEB_ADMIN_PASSWORD (optional)
if [ "$NON_INTERACTIVE" = "1" ]; then
    WEB_ADMIN_PASSWORD="${WEB_ADMIN_PASSWORD:-}"
else
    prompt_secret_optional WEB_ADMIN_PASSWORD "4/4  Web admin password ${DIM}(press Enter to auto-generate)${RESET}:"
fi

GENERATED_PASSWORD=0
if [ -z "$WEB_ADMIN_PASSWORD" ]; then
    WEB_ADMIN_PASSWORD="$(gen_password)"
    GENERATED_PASSWORD=1
    ok "A secure web admin password was generated (shown once at the end)."
fi

# --- derive the remaining boot values ---------------------------------------
info "Generating secrets and deriving configuration…"

# ADMIN_TELEGRAM_IDS defaults to the main admin.
ADMIN_TELEGRAM_IDS="$MAIN_ADMIN_TELEGRAM_ID"

# Scheme: default to https for a real domain; http for localhost/IP.
case "$WEB_PANEL_DOMAIN" in
    localhost|127.0.0.1|*:* ) SCHEME="http" ;;
    *[!0-9.]* ) SCHEME="https" ;;   # contains a non-IP char → treat as a hostname
    * ) SCHEME="http" ;;            # looks like a bare IPv4 address
esac
WEB_PANEL_URL="$SCHEME://$WEB_PANEL_DOMAIN"

POSTGRES_USER="digitalcore"
POSTGRES_DB="digitalcore"
POSTGRES_PASSWORD="$(gen_hex 24)"
DATABASE_URL="postgresql+asyncpg://${POSTGRES_USER}:${POSTGRES_PASSWORD}@postgres:5432/${POSTGRES_DB}"
REDIS_URL="redis://redis:6379/0"

SECRET_KEY="$(gen_hex 32)"
JWT_SECRET="$(gen_hex 32)"
FERNET_KEY="$(gen_fernet)"
BACKUP_ENCRYPTION_KEY="$(gen_fernet)"
ok "Secrets generated."

# --- write .env -------------------------------------------------------------
umask 077
cat > "$ENV_FILE" <<EOF
# Generated by install.sh — do not commit this file.
# Boot settings only. Business settings are configured from the admin panel.

# --- Boot settings ---
BOT_TOKEN=${BOT_TOKEN}
MAIN_ADMIN_TELEGRAM_ID=${MAIN_ADMIN_TELEGRAM_ID}
ADMIN_TELEGRAM_IDS=${ADMIN_TELEGRAM_IDS}
DOMAIN=${WEB_PANEL_DOMAIN}
WEB_PANEL_URL=${WEB_PANEL_URL}

# --- Datastores ---
POSTGRES_USER=${POSTGRES_USER}
POSTGRES_PASSWORD=${POSTGRES_PASSWORD}
POSTGRES_DB=${POSTGRES_DB}
DATABASE_URL=${DATABASE_URL}
REDIS_URL=${REDIS_URL}

# --- Secrets ---
SECRET_KEY=${SECRET_KEY}
JWT_SECRET=${JWT_SECRET}
FERNET_KEY=${FERNET_KEY}
BACKUP_ENCRYPTION_KEY=${BACKUP_ENCRYPTION_KEY}

# --- Runtime flags ---
MAINTENANCE_MODE=false

# --- Web admin bootstrap (consumed by the seeder on first boot) ---
WEB_ADMIN_USERNAME=admin
WEB_ADMIN_PASSWORD=${WEB_ADMIN_PASSWORD}

# --- Optional business defaults (empty; configured from the panel) ---
LOG_GROUP_ID=
FORCE_JOIN_CHANNEL=
DEFAULT_CARD_NUMBER=
DEFAULT_CARD_OWNER=
DEFAULT_SHEBA=
EOF
umask 022
ok "Wrote $ENV_FILE"

# --- build & start ----------------------------------------------------------
info "Building images (this can take a few minutes the first time)…"
$COMPOSE --env-file "$ENV_FILE" build

info "Starting the platform…"
$COMPOSE --env-file "$ENV_FILE" up -d

# --- wait for the web service to become healthy -----------------------------
info "Waiting for the web panel to become healthy…"
HEALTHY=0
for _ in $(seq 1 60); do
    state="$($COMPOSE --env-file "$ENV_FILE" ps -q web 2>/dev/null | xargs -r docker inspect -f '{{.State.Health.Status}}' 2>/dev/null || true)"
    if [ "$state" = "healthy" ]; then HEALTHY=1; break; fi
    sleep 2
done
if [ "$HEALTHY" = "1" ]; then
    ok "Web panel is up."
else
    warn "The web panel did not report healthy yet. Check logs with: $COMPOSE logs -f web"
fi

# The web container runs migrations + seeds the owner admin and the default
# (empty) business-settings records automatically on startup. See scripts/entrypoint.sh.

# --- summary ----------------------------------------------------------------
printf '\n%s\n' "${GREEN}${BOLD}DigitalCore is installed.${RESET}"
printf '%s\n' "────────────────────────────────────────────────────────"
printf '  %s %s\n' "${BOLD}Web panel:${RESET}" "${WEB_PANEL_URL}"
printf '  %s %s\n' "${BOLD}Login:${RESET}"     "username ${CYAN}admin${RESET} (or your Telegram ID ${MAIN_ADMIN_TELEGRAM_ID})"
if [ "$GENERATED_PASSWORD" = "1" ]; then
    printf '  %s %s\n' "${BOLD}Password:${RESET}" "${YELLOW}${WEB_ADMIN_PASSWORD}${RESET}  ${DIM}(shown once — save it now)${RESET}"
else
    printf '  %s %s\n' "${BOLD}Password:${RESET}" "the password you entered"
fi
printf '  %s %s\n' "${BOLD}Owner admin:${RESET}" "Telegram ID ${MAIN_ADMIN_TELEGRAM_ID}"
printf '%s\n' "────────────────────────────────────────────────────────"
printf '%s\n' "Next: open the panel, go to ${BOLD}Settings${RESET}, and configure your"
printf '%s\n' "cards, channels, plans, V2Ray/3X-UI servers, licenses and texts."
printf '%s\n\n' "The installer intentionally did not ask for any of those."

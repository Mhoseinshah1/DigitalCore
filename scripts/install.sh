#!/usr/bin/env bash
# =============================================================================
# DigitalCore — one-command production installer for Ubuntu
# =============================================================================
# Usage (on a fresh Ubuntu 22.04 / 24.04 server, amd64 or arm64):
#
#     curl -fsSL https://raw.githubusercontent.com/Mhoseinshah1/DigitalCore/main/scripts/install.sh | sudo bash
#
# Fully non-interactive (CI / automation) — pass values as environment vars:
#
#     curl -fsSL .../install.sh | sudo BOT_TOKEN=123:abc MAIN_ADMIN_TELEGRAM_ID=111 \
#         DOMAIN=panel.example.com ADMIN_USERNAME=admin NON_INTERACTIVE=1 bash
#
# The installer asks ONLY for: BOT_TOKEN, MAIN_ADMIN_TELEGRAM_ID, DOMAIN, the
# admin username (default "admin"), and an optional web-admin password.
# Everything else (card numbers, prices, products, 3X-UI servers, texts, …) is
# configured later from the admin panel.
#
# It installs Docker if missing, clones the repo to /opt/digitalcore, generates
# all secrets, brings the stack up, migrates, creates the admin, and verifies
# /health + /ready. It never claims success unless the app is actually healthy,
# and never silently exits.
# =============================================================================
# -E makes the ERR trap fire inside functions/subshells too, so a failure
# anywhere is reported with its line and command rather than exiting silently.
set -Eeuo pipefail

# --- configuration (override via env if needed) ------------------------------
REPO_URL="${REPO_URL:-https://github.com/Mhoseinshah1/DigitalCore.git}"
REPO_BRANCH="${REPO_BRANCH:-main}"
INSTALL_DIR="${INSTALL_DIR:-/opt/digitalcore}"
NON_INTERACTIVE="${NON_INTERACTIVE:-0}"

# --- pretty output -----------------------------------------------------------
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

# --- error trap: never exit silently ----------------------------------------
COMPOSE=""
on_error() {
    # Capture status first; $LINENO and $BASH_COMMAND are passed in from the trap.
    local code=$? line="${1:-?}" cmd="${2:-?}"
    printf '\n%s\n' "${RED}${BOLD}Installation FAILED (exit ${code}).${RESET}" >&2
    printf '%s\n' "${RED}  Failed at line ${line}: ${cmd}${RESET}" >&2
    if [ -n "$COMPOSE" ] && [ -d "$INSTALL_DIR" ]; then
        warn "Container status (${COMPOSE} ps -a):"
        ( cd "$INSTALL_DIR" && $COMPOSE ps -a 2>/dev/null || true )
        for svc in backend bot worker postgres redis; do
            warn "Last logs for '${svc}':"
            ( cd "$INSTALL_DIR" && $COMPOSE logs "$svc" --tail=80 2>/dev/null || true )
        done
    fi
    printf '%s\n' "See the messages above. Re-running the installer is safe (it keeps your .env)." >&2
    exit "$code"
}
# Pass the failing line and command so the report is specific, never silent.
trap 'on_error "$LINENO" "$BASH_COMMAND"' ERR

# --- helpers -----------------------------------------------------------------
# Read from the real terminal even when the script is piped from curl.
ask() {
    # ask VAR "Question" "default" [secret]
    local __var="$1" __q="$2" __def="${3:-}" __secret="${4:-}" __val=""
    # If a value already came from the environment, use it as-is.
    if [ -n "${!__var:-}" ]; then printf -v "$__var" '%s' "${!__var}"; return; fi
    if [ "$NON_INTERACTIVE" = "1" ] || [ ! -r /dev/tty ]; then
        printf -v "$__var" '%s' "$__def"; return
    fi
    if [ -n "$__secret" ]; then
        printf '%s' "${BOLD}$__q${RESET} " > /dev/tty
        IFS= read -rs __val < /dev/tty || true; printf '\n' > /dev/tty
    else
        if [ -n "$__def" ]; then printf '%s' "${BOLD}$__q${RESET} [${__def}] " > /dev/tty
        else printf '%s' "${BOLD}$__q${RESET} " > /dev/tty; fi
        IFS= read -r __val < /dev/tty || true
    fi
    [ -z "$__val" ] && __val="$__def"
    printf -v "$__var" '%s' "$__val"
}

# set_env KEY VALUE — replace or append KEY=VALUE in .env (value taken literally).
set_env() {
    local key="$1" val="$2"
    if grep -qE "^${key}=" .env; then
        awk -v k="$key" -v v="$val" '
            BEGIN{done=0}
            {
                if ($0 ~ "^" k "=") { print k "=" v; done=1 }
                else { print }
            }' .env > .env.tmp && mv .env.tmp .env
    else
        printf '%s=%s\n' "$key" "$val" >> .env
    fi
}

gen_hex()    { openssl rand -hex 32; }                                  # url-safe (0-9a-f)
gen_fernet() { head -c 32 /dev/urandom | base64 | tr '+/' '-_'; }        # 44-char urlsafe base64

# --- 0. privilege & OS -------------------------------------------------------
[ "$(id -u)" -eq 0 ] || die "Please run as root:  curl -fsSL <url> | sudo bash"

info "Checking the operating system…"
if [ -r /etc/os-release ]; then
    . /etc/os-release
    if [ "${ID:-}" = "ubuntu" ]; then
        case "${VERSION_ID:-}" in
            22.04|24.04) ok "Ubuntu ${VERSION_ID} detected." ;;
            *) warn "Tested on Ubuntu 22.04/24.04; detected ${VERSION_ID:-unknown}. Continuing." ;;
        esac
    else
        warn "This installer targets Ubuntu; detected '${ID:-unknown}'. Continuing anyway."
    fi
else
    warn "Could not detect the OS (no /etc/os-release). Continuing anyway."
fi
ARCH="$(dpkg --print-architecture 2>/dev/null || uname -m)"
case "$ARCH" in
    amd64|x86_64|arm64|aarch64) ok "Architecture: ${ARCH}." ;;
    *) warn "Untested architecture '${ARCH}'. Continuing." ;;
esac

# --- 1. base packages --------------------------------------------------------
info "Installing base packages (git, curl, openssl, ca-certificates)…"
export DEBIAN_FRONTEND=noninteractive
apt-get update -qq
apt-get install -y -qq git curl ca-certificates openssl >/dev/null
ok "Base packages ready."

# --- 2. Docker + compose plugin ---------------------------------------------
if docker info >/dev/null 2>&1; then
    ok "Docker is already installed and running."
else
    info "Installing Docker Engine…"
    curl -fsSL https://get.docker.com | sh >/dev/null 2>&1 || die "Docker installation failed."
    systemctl enable --now docker >/dev/null 2>&1 || true
    docker info >/dev/null 2>&1 || die "Docker is installed but the daemon is not running."
    ok "Docker installed."
fi
if docker compose version >/dev/null 2>&1; then
    COMPOSE="docker compose"
elif command -v docker-compose >/dev/null 2>&1; then
    COMPOSE="docker-compose"
else
    info "Installing the Docker Compose plugin…"
    apt-get install -y -qq docker-compose-plugin >/dev/null 2>&1 || true
    docker compose version >/dev/null 2>&1 || die "Docker Compose plugin could not be installed."
    COMPOSE="docker compose"
fi
ok "Docker Compose is available (${COMPOSE})."

# --- 3. clone / update repo --------------------------------------------------
if [ -d "$INSTALL_DIR/.git" ]; then
    info "Updating existing checkout at ${INSTALL_DIR}…"
    git -C "$INSTALL_DIR" fetch --depth 1 origin "$REPO_BRANCH" >/dev/null 2>&1 || true
    git -C "$INSTALL_DIR" checkout "$REPO_BRANCH" >/dev/null 2>&1 || true
    git -C "$INSTALL_DIR" reset --hard "origin/${REPO_BRANCH}" >/dev/null 2>&1 || true
    ok "Repository updated."
else
    info "Cloning ${REPO_URL} → ${INSTALL_DIR}…"
    mkdir -p "$(dirname "$INSTALL_DIR")"
    git clone --depth 1 --branch "$REPO_BRANCH" "$REPO_URL" "$INSTALL_DIR" >/dev/null 2>&1 \
        || die "git clone failed (check the repo URL / network)."
    ok "Repository cloned."
fi
cd "$INSTALL_DIR"
[ -f .env.example ] || die ".env.example is missing in the repo; cannot continue."
[ -f docker-compose.yml ] || die "docker-compose.yml is missing in the repo; cannot continue."

# --- 4. .env -----------------------------------------------------------------
if [ -f .env ]; then
    ok "Existing .env found — keeping it (secrets are preserved). Edit it by hand to change values."
    ADMIN_USERNAME_OUT="$(grep -E '^ADMIN_USERNAME=' .env | cut -d= -f2- || true)"
    ADMIN_USERNAME_OUT="${ADMIN_USERNAME_OUT:-admin}"
    ADMIN_PW_OUT="(unchanged — see your existing .env)"
else
    cp .env.example .env
    ok "Created .env from .env.example."

    printf '\n%s\n' "${BOLD}A few values are needed (press Enter to skip optional ones):${RESET}"
    ask BOT_TOKEN                "Telegram BOT_TOKEN (optional):"        ""
    ask MAIN_ADMIN_TELEGRAM_ID   "Main admin Telegram numeric ID:"       ""
    ask DOMAIN                   "Web panel domain (optional):"          ""
    ask ADMIN_USERNAME           "Admin username:"                       "admin"
    ask WEB_ADMIN_PASSWORD       "Web admin password (blank = auto):"    "" secret

    # Generated secrets (first install only).
    POSTGRES_USER_V="$(grep -E '^POSTGRES_USER=' .env | cut -d= -f2- || echo digitalcore)"
    POSTGRES_DB_V="$(grep -E '^POSTGRES_DB=' .env | cut -d= -f2- || echo digitalcore)"
    POSTGRES_USER_V="${POSTGRES_USER_V:-digitalcore}"
    POSTGRES_DB_V="${POSTGRES_DB_V:-digitalcore}"

    PG_PASS="$(gen_hex)"
    SECRET_KEY_V="$(gen_hex)"
    JWT_SECRET_V="$(gen_hex)"
    BACKUP_KEY_V="$(gen_hex)"
    FERNET_V="$(gen_fernet)"
    DB_URL="postgresql+asyncpg://${POSTGRES_USER_V}:${PG_PASS}@postgres:5432/${POSTGRES_DB_V}"

    # Admin credentials for the web panel (username scheme; email is optional).
    ADMIN_USERNAME="${ADMIN_USERNAME:-admin}"
    ADMIN_USERNAME_OUT="$ADMIN_USERNAME"
    if [ -z "${WEB_ADMIN_PASSWORD:-}" ]; then
        WEB_ADMIN_PASSWORD="$(openssl rand -base64 12 | tr -d '/+=' | cut -c1-16)"
        ADMIN_PW_OUT="$WEB_ADMIN_PASSWORD"
    else
        ADMIN_PW_OUT="(the password you entered)"
    fi

    if [ -n "${DOMAIN:-}" ]; then WEB_PANEL_URL_V="https://${DOMAIN}"; else WEB_PANEL_URL_V=""; fi

    # Write everything into .env.
    set_env APP_ENV               "production"
    set_env POSTGRES_PASSWORD     "$PG_PASS"
    set_env DATABASE_URL          "$DB_URL"
    set_env REDIS_URL             "redis://redis:6379/0"
    set_env SECRET_KEY            "$SECRET_KEY_V"
    set_env JWT_SECRET            "$JWT_SECRET_V"
    set_env FERNET_KEY            "$FERNET_V"
    set_env BACKUP_ENCRYPTION_KEY "$BACKUP_KEY_V"
    set_env WEB_PANEL_URL         "$WEB_PANEL_URL_V"
    set_env ADMIN_USERNAME        "$ADMIN_USERNAME"
    set_env ADMIN_PASSWORD        "$WEB_ADMIN_PASSWORD"
    set_env TELEGRAM_BOT_TOKEN    "${BOT_TOKEN:-}"
    set_env TELEGRAM_ADMIN_ID     "${MAIN_ADMIN_TELEGRAM_ID:-}"
    [ -n "${DOMAIN:-}" ] && set_env DOMAIN "$DOMAIN"
    chmod 600 .env
    ok "Wrote configuration to .env (secrets generated, file locked to 0600)."
fi

# Which host port is the backend published on?
API_PORT="$(grep -E '^API_PORT=' .env | cut -d= -f2- || true)"; API_PORT="${API_PORT:-8000}"
HEALTH_URL="http://localhost:${API_PORT}/health"
READY_URL="http://localhost:${API_PORT}/ready"

# --- 5. build & start --------------------------------------------------------
info "Building and starting the stack (this can take a few minutes)…"
$COMPOSE up -d --build

# --- 6. migrate --------------------------------------------------------------
info "Applying database migrations…"
$COMPOSE exec -T backend alembic upgrade head

# --- 7. create admin ---------------------------------------------------------
info "Creating the web admin…"
$COMPOSE exec -T backend python scripts/create_admin.py

# --- 8. health / readiness gate ---------------------------------------------
info "Waiting for the backend to become healthy…"
health_ok=""
for _ in $(seq 1 45); do
    if curl -fsS "$HEALTH_URL" >/dev/null 2>&1; then health_ok="yes"; break; fi
    sleep 2
done
[ -n "$health_ok" ] || die "Backend /health did not pass in time."
ok "/health passed."

info "Checking readiness (database + redis)…"
ready_ok=""
for _ in $(seq 1 45); do
    if curl -fsS "$READY_URL" >/dev/null 2>&1; then ready_ok="yes"; break; fi
    sleep 2
done
[ -n "$ready_ok" ] || die "Backend /ready did not pass (database/redis not ready)."
ok "/ready passed."

# --- 9. installation summary ---------------------------------------------------
# Secrets (SECRET_KEY / JWT_SECRET / FERNET_KEY / POSTGRES_PASSWORD /
# BACKUP_ENCRYPTION_KEY / bot token value) are intentionally NEVER printed.
DOMAIN_V="$(grep -E '^DOMAIN=' .env | cut -d= -f2- || true)"
TG_ADMIN_V="$(grep -E '^TELEGRAM_ADMIN_ID=' .env | cut -d= -f2- || true)"
BOT_TOKEN_V="$(grep -E '^TELEGRAM_BOT_TOKEN=' .env | cut -d= -f2- || true)"
if [ -z "${ADMIN_USERNAME_OUT:-}" ]; then
    ADMIN_USERNAME_OUT="$(grep -E '^ADMIN_USERNAME=' .env | cut -d= -f2- || true)"
    ADMIN_USERNAME_OUT="${ADMIN_USERNAME_OUT:-admin}"
fi
PANEL_URL="http://localhost:${API_PORT}"

printf '\n%s\n' "${GREEN}${BOLD}DigitalCore is installed and healthy.${RESET}"
printf '%s\n' "${BOLD}─────────────── Installation summary ───────────────${RESET}"
printf '  Location:           %s\n' "$INSTALL_DIR"
printf '  Panel URL:          %s   (HTTP only — Nginx/HTTPS is a later phase)\n' "$PANEL_URL"
if [ -n "$DOMAIN_V" ]; then
    printf '  Panel (domain):     http://%s:%s\n' "$DOMAIN_V" "$API_PORT"
fi
printf '  Login page:         %s/login\n' "$PANEL_URL"
printf '  Admin username:     %s\n' "$ADMIN_USERNAME_OUT"
printf '  Admin password:     %s\n' "${ADMIN_PW_OUT:-(the password you set)}"
printf '  Telegram admin ID:  %s\n' "${TG_ADMIN_V:-not set}"
if [ -n "$BOT_TOKEN_V" ]; then
    printf '  Bot token:          configured\n'
else
    printf '  Bot token:          not set\n'
fi
printf '  Secrets file:       %s/.env (mode 600) — back it up safely.\n' "$INSTALL_DIR"
printf '%s\n' "${BOLD}─────────────── Management commands ────────────────${RESET}"
printf '  Update:    cd %s && sudo bash scripts/update.sh\n' "$INSTALL_DIR"
printf '  Backup:    sudo bash scripts/backup.sh\n'
printf '  Restore:   sudo bash scripts/restore.sh --latest\n'
printf '  Health:    bash scripts/healthcheck.sh\n'
printf '  Status:    %s ps\n' "$COMPOSE"
printf '  Logs:      %s logs backend --tail=100\n\n' "$COMPOSE"

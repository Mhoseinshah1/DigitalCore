#!/usr/bin/env bash
# =============================================================================
# DigitalCore — safe update with automatic code rollback
# =============================================================================
# Takes an encrypted backup, pulls the latest code, rebuilds, migrates, and gates
# on /health + /ready. If the build, migration or health check fails, it rolls the
# CODE back to the previous commit and rebuilds.
#
# By design ONLY the code is auto-rolled-back. A DB rollback (if a migration
# changed the schema) is manual, via restore.sh with the backup this script made.
#
#   Env: REPO_BRANCH (default main)
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

# --- preconditions -----------------------------------------------------------
[ -d "$ROOT_DIR/.git" ] || die "Not a git checkout ($ROOT_DIR) — cannot update."
[ -f "$ENV_FILE" ] || die ".env not found — refusing to update an unconfigured install."
command -v git >/dev/null 2>&1 || die "git is not installed."

REPO_BRANCH="${REPO_BRANCH:-main}"
API_PORT="$(env_get API_PORT 8000)"
HEALTH_URL="http://localhost:${API_PORT}/health"
READY_URL="http://localhost:${API_PORT}/ready"

poll_http() {
    # poll_http URL [tries] -> 0 when it returns 200
    local url="$1" tries="${2:-45}" i code
    for ((i = 0; i < tries; i++)); do
        code="$(curl -s -o /dev/null -w '%{http_code}' --max-time 5 "$url" 2>/dev/null || true)"
        [ "$code" = "200" ] && return 0
        sleep 2
    done
    return 1
}

PREV_COMMIT="$(git rev-parse HEAD)"
BACKUP_PATH=""

rollback_code() {
    warn "Update failed — rolling the CODE back to ${PREV_COMMIT}…"
    git reset --hard "$PREV_COMMIT" >/dev/null 2>&1 || warn "git reset to previous commit failed."
    if "${COMPOSE[@]}" up -d --build; then
        if poll_http "$HEALTH_URL" && poll_http "$READY_URL"; then
            ok "Code rolled back to ${PREV_COMMIT}; the app is healthy again."
        else
            warn "Rolled back the code but health is still not green — inspect logs: bash scripts/healthcheck.sh"
        fi
    else
        warn "Rollback rebuild failed — inspect logs: bash scripts/healthcheck.sh"
    fi
    if [ -n "$BACKUP_PATH" ]; then
        printf '%s\n' "${BOLD}If a migration changed the schema, restore the DB with:${RESET}"
        printf '%s\n' "    sudo bash scripts/restore.sh ${BACKUP_PATH} --yes"
    fi
    exit 1
}

# --- 1. pre-update backup (abort if it fails) --------------------------------
info "Taking a pre-update encrypted backup…"
if ! BACKUP_PATH="$(bash "$ROOT_DIR/scripts/backup.sh" | tail -n1)"; then
    die "Pre-update backup failed — aborting the update (no changes made)."
fi
[ -n "$BACKUP_PATH" ] && [ -f "$BACKUP_PATH" ] || die "Backup path not produced — aborting."
ok "Backup ready: $(basename "$BACKUP_PATH")"

# --- 2. fetch + fast-forward the code ---------------------------------------
info "Fetching origin/${REPO_BRANCH}…"
git fetch --depth 50 origin "$REPO_BRANCH" || die "git fetch failed — aborting (no changes made)."
info "Updating working tree to origin/${REPO_BRANCH}…"
git reset --hard "origin/${REPO_BRANCH}" || die "git reset failed — aborting."
NEW_COMMIT="$(git rev-parse HEAD)"

if [ "$NEW_COMMIT" = "$PREV_COMMIT" ]; then
    ok "Already up to date at ${NEW_COMMIT}. Rebuilding to be safe…"
else
    info "Updating ${PREV_COMMIT} → ${NEW_COMMIT}."
fi

# --- 3. build ----------------------------------------------------------------
info "Building and starting the stack…"
if ! "${COMPOSE[@]}" up -d --build; then
    rollback_code
fi

# --- 4. migrate --------------------------------------------------------------
info "Applying database migrations…"
if ! "${COMPOSE[@]}" exec -T backend alembic upgrade head; then
    rollback_code
fi

# --- 5. health gate ----------------------------------------------------------
info "Waiting for /health…"
if ! poll_http "$HEALTH_URL"; then
    rollback_code
fi
info "Waiting for /ready…"
if ! poll_http "$READY_URL"; then
    rollback_code
fi

# --- 6. success --------------------------------------------------------------
bash "$ROOT_DIR/scripts/healthcheck.sh" || true
printf '\n%s\n' "${GREEN}${BOLD}Update complete — now at ${NEW_COMMIT} and healthy.${RESET}"
printf '  Previous commit: %s\n' "$PREV_COMMIT"
printf '  Backup:          %s\n' "$BACKUP_PATH"

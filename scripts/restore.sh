#!/usr/bin/env bash
# =============================================================================
# DigitalCore — restore from an encrypted backup  (DESTRUCTIVE)
# =============================================================================
# Usage:
#   sudo bash scripts/restore.sh <backup.tar.gz.enc> [--yes]
#   sudo bash scripts/restore.sh --latest [--yes]
#
# Decrypts and restores the database and storage/ files from a backup produced
# by backup.sh. This OVERWRITES the current database. Without --yes it asks for
# an explicit 'yes' confirmation read from the terminal.
# =============================================================================
set -euo pipefail

# --- messaging ---------------------------------------------------------------
if [ -t 1 ]; then
    BOLD=$'\033[1m'; RED=$'\033[31m'; GREEN=$'\033[32m'; YELLOW=$'\033[33m'; CYAN=$'\033[36m'; RESET=$'\033[0m'
else
    BOLD=""; RED=""; GREEN=""; YELLOW=""; CYAN=""; RESET=""
fi
info() { printf '%s\n' "${CYAN}==>${RESET} $*" >&2; }
ok()   { printf '%s\n' "${GREEN}✓${RESET} $*" >&2; }
warn() { printf '%s\n' "${YELLOW}!${RESET} $*" >&2; }
die()  { printf '%s\n' "${RED}✗ $*${RESET}" >&2; exit 1; }

# --- locate repo root --------------------------------------------------------
ROOT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
cd "$ROOT_DIR"
ENV_FILE="$ROOT_DIR/.env"
BACKUP_DIR="$ROOT_DIR/storage/backups"

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

# --- parse args --------------------------------------------------------------
BACKUP_FILE=""
USE_LATEST=0
SKIP_CONFIRM=0
for arg in "$@"; do
    case "$arg" in
        --latest) USE_LATEST=1 ;;
        --yes|-y) SKIP_CONFIRM=1 ;;
        -h|--help)
            sed -n '2,14p' "$0"; exit 0 ;;
        -*) die "Unknown option: $arg" ;;
        *)  BACKUP_FILE="$arg" ;;
    esac
done

if [ "$USE_LATEST" = "1" ]; then
    BACKUP_FILE="$(
        find "$BACKUP_DIR" -maxdepth 1 -type f -name '*.tar.gz.enc' -printf '%T@ %p\n' 2>/dev/null \
            | sort -rn | head -n1 | cut -d' ' -f2-
    )"
    [ -n "$BACKUP_FILE" ] || die "No backups found in $BACKUP_DIR."
fi

[ -n "$BACKUP_FILE" ] || die "Provide a backup path or --latest. See --help."
[ -f "$BACKUP_FILE" ] || die "Backup file not found: $BACKUP_FILE"

BACKUP_KEY="$(env_get BACKUP_ENCRYPTION_KEY)"
PGUSER="$(env_get POSTGRES_USER digitalcore)"
PGDB="$(env_get POSTGRES_DB digitalcore)"
[ -n "$BACKUP_KEY" ] || die "BACKUP_ENCRYPTION_KEY is empty in .env — cannot decrypt."
command -v openssl >/dev/null 2>&1 || die "openssl is not installed."

# --- confirmation ------------------------------------------------------------
if [ "$SKIP_CONFIRM" != "1" ]; then
    warn "This will OVERWRITE the current database and storage files from:"
    printf '     %s\n' "$BACKUP_FILE" >&2
    if [ -r /dev/tty ]; then
        printf '%s' "${BOLD}Type 'yes' to continue: ${RESET}" >&2
        IFS= read -r reply < /dev/tty || true
    else
        die "No terminal for confirmation; re-run with --yes to proceed non-interactively."
    fi
    [ "$reply" = "yes" ] || die "Aborted (no confirmation)."
fi

# --- workspace ---------------------------------------------------------------
TMP="$(mktemp -d)"
trap 'rm -rf "$TMP"' EXIT

# --- 1. decrypt + extract ----------------------------------------------------
info "Decrypting backup…"
if ! openssl enc -d -aes-256-cbc -pbkdf2 -pass "pass:$BACKUP_KEY" -in "$BACKUP_FILE" -out "$TMP/archive.tar.gz"; then
    die "Decryption failed (wrong BACKUP_ENCRYPTION_KEY or corrupt file)."
fi
mkdir -p "$TMP/extract"
if ! tar -xzf "$TMP/archive.tar.gz" -C "$TMP/extract"; then
    die "Extraction failed (corrupt archive)."
fi
[ -f "$TMP/extract/db.sql" ] || die "db.sql not found in the backup — nothing to restore."
ok "Backup decrypted and extracted."

# --- 2. stop app services (keep postgres up) ---------------------------------
info "Stopping backend, bot and worker (postgres stays up)…"
"${COMPOSE[@]}" stop backend bot worker >/dev/null 2>&1 || true

# --- 3. restore the database -------------------------------------------------
info "Restoring database '${PGDB}'…"
if ! "${COMPOSE[@]}" exec -T postgres psql -U "$PGUSER" -d "$PGDB" < "$TMP/extract/db.sql" >/dev/null; then
    die "Database restore failed."
fi
ok "Database restored."

# --- 4. restore storage/ files ----------------------------------------------
if [ -d "$TMP/extract/storage" ]; then
    info "Restoring storage/ files…"
    mkdir -p "$ROOT_DIR/storage"
    for d in receipts exports logs; do
        if [ -d "$TMP/extract/storage/$d" ]; then
            rm -rf "$ROOT_DIR/storage/$d"
            cp -a "$TMP/extract/storage/$d" "$ROOT_DIR/storage/$d"
        fi
    done
    ok "storage/ files restored."
else
    warn "No storage/ payload in the backup — skipping file restore."
fi

# --- 5. bring the stack back up ---------------------------------------------
info "Starting the stack…"
"${COMPOSE[@]}" up -d

# --- 6. readiness ------------------------------------------------------------
API_PORT="$(env_get API_PORT 8000)"
READY_URL="http://localhost:${API_PORT}/ready"
info "Waiting for readiness…"
ready_ok=""
for _ in $(seq 1 45); do
    code="$(curl -s -o /dev/null -w '%{http_code}' --max-time 5 "$READY_URL" 2>/dev/null || true)"
    if [ "$code" = "200" ]; then ready_ok="yes"; break; fi
    sleep 2
done
if [ -n "$ready_ok" ]; then
    ok "Restore complete — /ready is green."
else
    warn "Restore finished but /ready is not green yet. Check: bash scripts/healthcheck.sh"
fi

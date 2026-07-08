#!/usr/bin/env bash
# =============================================================================
# DigitalCore — restore from a backup  (DESTRUCTIVE)
# =============================================================================
# Usage:
#   sudo bash scripts/restore.sh <backup-file> [--yes]
#   sudo bash scripts/restore.sh --latest [--yes]
#
# Restores the database and/or storage/ from a backup produced by backup.sh or
# by the admin panel:
#     *.sql.gz    → database only
#     full *.tar.gz (contains database/ + storage/) → database + storage
#     storage *.tar.gz (storage/ only) → storage only
#     *.tar.gz.enc → legacy encrypted archive (needs BACKUP_ENCRYPTION_KEY)
#
# This OVERWRITES the current database. It is guarded:
#   1. A fresh PRE-RESTORE backup is taken first (rollback point).
#   2. You must type exactly  RESTORE_DIGITALCORE  to proceed (unless --yes).
# On any failure, existing backups are left untouched.
#
# No secrets are printed; the DB password is never placed on a command line.
# =============================================================================
set -euo pipefail

CONFIRM_PHRASE="RESTORE_DIGITALCORE"

if [ -t 1 ]; then
    BOLD=$'\033[1m'; RED=$'\033[31m'; GREEN=$'\033[32m'; YELLOW=$'\033[33m'; CYAN=$'\033[36m'; RESET=$'\033[0m'
else
    BOLD=""; RED=""; GREEN=""; YELLOW=""; CYAN=""; RESET=""
fi
info() { printf '%s\n' "${CYAN}==>${RESET} $*" >&2; }
ok()   { printf '%s\n' "${GREEN}✓${RESET} $*" >&2; }
warn() { printf '%s\n' "${YELLOW}!${RESET} $*" >&2; }
die()  { printf '%s\n' "${RED}✗ $*${RESET}" >&2; exit 1; }

ROOT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
cd "$ROOT_DIR"
ENV_FILE="$ROOT_DIR/.env"
BACKUP_DIR="$ROOT_DIR/storage/backups"

env_get() {
    local key="$1" default="${2:-}" line val
    [ -f "$ENV_FILE" ] || { printf '%s' "$default"; return; }
    line="$(grep -E "^${key}=" "$ENV_FILE" | head -n1 || true)"
    [ -n "$line" ] || { printf '%s' "$default"; return; }
    val="${line#*=}"; val="${val%$'\r'}"
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
BACKUP_FILE=""; USE_LATEST=0; SKIP_CONFIRM=0
for arg in "$@"; do
    case "$arg" in
        --latest) USE_LATEST=1 ;;
        --yes|-y) SKIP_CONFIRM=1 ;;
        -h|--help) sed -n '2,20p' "$0"; exit 0 ;;
        -*) die "Unknown option: $arg" ;;
        *)  BACKUP_FILE="$arg" ;;
    esac
done

if [ "$USE_LATEST" = "1" ]; then
    BACKUP_FILE="$(
        find "$BACKUP_DIR" -type f \( -name '*.tar.gz' -o -name '*.sql.gz' -o -name '*.tar.gz.enc' \) \
            -printf '%T@ %p\n' 2>/dev/null | sort -rn | head -n1 | cut -d' ' -f2-
    )"
    [ -n "$BACKUP_FILE" ] || die "No backups found in $BACKUP_DIR."
fi

[ -n "$BACKUP_FILE" ] || die "Provide a backup path or --latest. See --help."
[ -f "$BACKUP_FILE" ] || die "Backup file not found: $BACKUP_FILE"

PGUSER="$(env_get POSTGRES_USER digitalcore)"
PGDB="$(env_get POSTGRES_DB digitalcore)"

# --- confirmation ------------------------------------------------------------
if [ "$SKIP_CONFIRM" != "1" ]; then
    warn "This will OVERWRITE the current database and storage from:"
    printf '     %s\n' "$BACKUP_FILE" >&2
    warn "A pre-restore backup will be taken first."
    if [ -r /dev/tty ]; then
        printf '%s' "${BOLD}Type '${CONFIRM_PHRASE}' to continue: ${RESET}" >&2
        IFS= read -r reply < /dev/tty || true
    else
        die "No terminal for confirmation; re-run with --yes to proceed non-interactively."
    fi
    [ "$reply" = "$CONFIRM_PHRASE" ] || die "Aborted (confirmation phrase did not match)."
fi

# --- 1. pre-restore backup (rollback point) ----------------------------------
info "Taking a pre-restore backup…"
if ! PRE_BACKUP="$(bash "$ROOT_DIR/scripts/backup.sh" full | tail -n1)"; then
    die "Pre-restore backup failed — aborting the restore (no changes made)."
fi
ok "Pre-restore backup: $PRE_BACKUP"

# --- 2. workspace + extract --------------------------------------------------
TMP="$(mktemp -d)"; trap 'rm -rf "$TMP"' EXIT
mkdir -p "$TMP/extract"

case "$BACKUP_FILE" in
    *.tar.gz.enc)
        BACKUP_KEY="$(env_get BACKUP_ENCRYPTION_KEY)"
        [ -n "$BACKUP_KEY" ] || die "BACKUP_ENCRYPTION_KEY is empty — cannot decrypt this legacy backup."
        command -v openssl >/dev/null 2>&1 || die "openssl is not installed."
        info "Decrypting legacy archive…"
        openssl enc -d -aes-256-cbc -pbkdf2 -pass "pass:$BACKUP_KEY" \
            -in "$BACKUP_FILE" -out "$TMP/archive.tar.gz" || die "Decryption failed."
        tar -xzf "$TMP/archive.tar.gz" -C "$TMP/extract" || die "Extraction failed."
        ;;
    *.sql.gz)
        gunzip -c "$BACKUP_FILE" > "$TMP/extract/db.sql" || die "Failed to decompress SQL dump."
        ;;
    *.tar.gz)
        tar -xzf "$BACKUP_FILE" -C "$TMP/extract" || die "Extraction failed (corrupt archive)."
        ;;
    *) die "Unrecognised backup file type: $BACKUP_FILE" ;;
esac

# Locate a SQL dump inside the extracted payload (plain or gzipped).
DB_SQL=""
if [ -f "$TMP/extract/db.sql" ]; then
    DB_SQL="$TMP/extract/db.sql"
elif ls "$TMP"/extract/database/*.sql.gz >/dev/null 2>&1; then
    gunzip -c "$TMP"/extract/database/*.sql.gz > "$TMP/extract/db.sql"
    DB_SQL="$TMP/extract/db.sql"
elif [ -f "$TMP/extract/db.sql.gz" ]; then
    gunzip -c "$TMP/extract/db.sql.gz" > "$TMP/extract/db.sql"; DB_SQL="$TMP/extract/db.sql"
fi

# --- 3. stop app services (keep postgres up) ---------------------------------
info "Stopping backend, bot and worker (postgres stays up)…"
"${COMPOSE[@]}" stop backend bot worker >/dev/null 2>&1 || true

# --- 4. restore database (if the backup has one) -----------------------------
if [ -n "$DB_SQL" ]; then
    info "Restoring database '${PGDB}'…"
    if "${COMPOSE[@]}" ps postgres >/dev/null 2>&1; then
        "${COMPOSE[@]}" exec -T postgres psql -U "$PGUSER" -d "$PGDB" < "$DB_SQL" >/dev/null \
            || die "Database restore failed."
    else
        psql -U "$PGUSER" -d "$PGDB" < "$DB_SQL" >/dev/null || die "Database restore failed."
    fi
    ok "Database restored."
else
    warn "No database dump in this backup — skipping DB restore (storage only)."
fi

# --- 5. restore storage/ files ----------------------------------------------
if [ -d "$TMP/extract/storage" ]; then
    info "Restoring storage/ files…"
    for d in receipts tickets exports qrcodes uploads logs; do
        if [ -d "$TMP/extract/storage/$d" ]; then
            rm -rf "$ROOT_DIR/storage/$d"
            cp -a "$TMP/extract/storage/$d" "$ROOT_DIR/storage/$d"
        fi
    done
    ok "storage/ files restored."
else
    warn "No storage/ payload in the backup — skipping file restore."
fi

# --- 6. bring the stack back up + readiness ----------------------------------
info "Starting the stack…"
"${COMPOSE[@]}" up -d
API_PORT="$(env_get API_PORT 8000)"
info "Waiting for readiness…"
ready_ok=""
for _ in $(seq 1 45); do
    code="$(curl -s -o /dev/null -w '%{http_code}' --max-time 5 "http://localhost:${API_PORT}/ready" 2>/dev/null || true)"
    if [ "$code" = "200" ]; then ready_ok="yes"; break; fi
    sleep 2
done
if [ -n "$ready_ok" ]; then
    ok "Restore complete — /ready is green. (Turn maintenance mode off when verified.)"
else
    warn "Restore finished but /ready is not green yet. Check: bash scripts/healthcheck.sh"
fi

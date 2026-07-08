#!/usr/bin/env bash
# =============================================================================
# DigitalCore — backup  (database | storage | full)
# =============================================================================
# Writes a backup under storage/backups/YYYY/MM/ matching the in-app
# backup_service conventions, so the admin panel and list_backups.sh see the
# same files:
#     digitalcore-db-YYYYMMDD-HHMMSS.sql.gz      (database)
#     digitalcore-storage-YYYYMMDD-HHMMSS.tar.gz (storage)
#     digitalcore-full-YYYYMMDD-HHMMSS.tar.gz    (full: db + storage + metadata)
#
# Each file is written mode 0600 with a matching .sha256 sidecar. The ONLY thing
# printed to stdout is the final backup path (last line); all human messages go
# to stderr, so callers (update.sh) can capture the path.
#
# Usage:
#   ./scripts/backup.sh            # full (default)
#   ./scripts/backup.sh database
#   ./scripts/backup.sh storage
#   ./scripts/backup.sh full
#
# No secrets are ever printed. The database password is never passed on a
# command line; pg_dump runs inside the postgres container over its local
# socket.
# =============================================================================
set -euo pipefail

# --- messaging (stderr; stdout is reserved for the result path) --------------
if [ -t 2 ]; then
    RED=$'\033[31m'; GREEN=$'\033[32m'; YELLOW=$'\033[33m'; CYAN=$'\033[36m'; RESET=$'\033[0m'
else
    RED=""; GREEN=""; YELLOW=""; CYAN=""; RESET=""
fi
info() { printf '%s\n' "${CYAN}==>${RESET} $*" >&2; }
ok()   { printf '%s\n' "${GREEN}✓${RESET} $*" >&2; }
warn() { printf '%s\n' "${YELLOW}!${RESET} $*" >&2; }
die()  { printf '%s\n' "${RED}✗ $*${RESET}" >&2; exit 1; }

MODE="${1:-full}"
case "$MODE" in
    database|storage|full) ;;
    -h|--help) sed -n '2,25p' "$0"; exit 0 ;;
    *) die "Unknown mode '$MODE'. Use: database | storage | full." ;;
esac

# --- locate repo root --------------------------------------------------------
ROOT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
cd "$ROOT_DIR"
ENV_FILE="$ROOT_DIR/.env"

# --- read .env WITHOUT sourcing it -------------------------------------------
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
fi

PGUSER="$(env_get POSTGRES_USER digitalcore)"
PGDB="$(env_get POSTGRES_DB digitalcore)"

STAMP="$(date -u +%Y%m%d-%H%M%S)"
DEST_DIR="$ROOT_DIR/storage/backups/$(date -u +%Y)/$(date -u +%m)"
mkdir -p "$DEST_DIR"

TMP="$(mktemp -d)"
trap 'rm -rf "$TMP"' EXIT

# --- database dump helper (gzipped SQL) --------------------------------------
dump_db_gz() {
    local out="$1"
    if [ "${#COMPOSE[@]}" -gt 0 ] && "${COMPOSE[@]}" ps postgres >/dev/null 2>&1; then
        "${COMPOSE[@]}" exec -T postgres pg_dump -U "$PGUSER" -d "$PGDB" \
            --no-owner --no-privileges --clean --if-exists 2>/dev/null | gzip > "$out"
    elif command -v pg_dump >/dev/null 2>&1; then
        warn "postgres container not found — using local pg_dump."
        pg_dump -U "$PGUSER" -d "$PGDB" --no-owner --no-privileges --clean --if-exists \
            2>/dev/null | gzip > "$out"
    else
        die "Neither the postgres container nor a local pg_dump is available."
    fi
    [ -s "$out" ] || die "Database dump is empty — aborting."
}

collect_storage() {
    local target="$1" d args=()
    for d in receipts tickets exports qrcodes uploads; do
        [ -d "$ROOT_DIR/storage/$d" ] && args+=("storage/$d")
    done
    if [ "${#args[@]}" -gt 0 ]; then
        tar -czf "$target" -C "$ROOT_DIR" "${args[@]}"
    else
        warn "No storage subdirs to back up — writing an empty archive."
        : > "$TMP/EMPTY"
        tar -czf "$target" -C "$TMP" EMPTY
    fi
}

finalize() {
    local path="$1"
    chmod 600 "$path"
    if command -v sha256sum >/dev/null 2>&1; then
        ( cd "$(dirname "$path")" && sha256sum "$(basename "$path")" > "$path.sha256" )
        chmod 600 "$path.sha256"
    fi
    ok "Backup written: $(basename "$path") ($(wc -c < "$path") bytes, mode 0600)."
    printf '%s\n' "$path"
}

case "$MODE" in
    database)
        OUT="$DEST_DIR/digitalcore-db-${STAMP}.sql.gz"
        info "Dumping database '${PGDB}'…"
        dump_db_gz "$OUT"
        finalize "$OUT"
        ;;
    storage)
        OUT="$DEST_DIR/digitalcore-storage-${STAMP}.tar.gz"
        info "Archiving storage/…"
        collect_storage "$OUT"
        finalize "$OUT"
        ;;
    full)
        OUT="$DEST_DIR/digitalcore-full-${STAMP}.tar.gz"
        info "Building full backup (database + storage)…"
        PAYLOAD="$TMP/payload"; mkdir -p "$PAYLOAD/database"
        dump_db_gz "$PAYLOAD/database/digitalcore-db-${STAMP}.sql.gz"
        for d in receipts tickets exports qrcodes uploads; do
            if [ -d "$ROOT_DIR/storage/$d" ]; then
                mkdir -p "$PAYLOAD/storage"
                cp -a "$ROOT_DIR/storage/$d" "$PAYLOAD/storage/$d"
            fi
        done
        printf '{"kind":"full","tool":"pg_dump","created_at":"%s"}\n' \
            "$(date -u +%Y-%m-%dT%H:%M:%SZ)" > "$PAYLOAD/metadata.json"
        cat > "$PAYLOAD/RESTORE.txt" <<'TXT'
DigitalCore full backup — restore with:  bash scripts/restore.sh <this-archive>
(you will be asked to type RESTORE_DIGITALCORE; a pre-restore backup is made first).
TXT
        tar -czf "$OUT" -C "$PAYLOAD" .
        finalize "$OUT"
        ;;
esac

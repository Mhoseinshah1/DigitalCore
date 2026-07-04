#!/usr/bin/env bash
# =============================================================================
# DigitalCore — encrypted backup
# =============================================================================
# Dumps the PostgreSQL database and bundles .env + storage/{receipts,exports,logs}
# into a single AES-256 encrypted archive under storage/backups/.
#
# The ONLY thing printed to stdout is the final backup path (last line), so other
# scripts (update.sh) can capture it. All human messages go to stderr.
#
#   Env: BACKUP_KEEP (default 7)   — how many backups to retain.
# =============================================================================
set -euo pipefail

# --- messaging (to stderr; stdout is reserved for the result path) -----------
if [ -t 2 ]; then
    RED=$'\033[31m'; GREEN=$'\033[32m'; YELLOW=$'\033[33m'; CYAN=$'\033[36m'; RESET=$'\033[0m'
else
    RED=""; GREEN=""; YELLOW=""; CYAN=""; RESET=""
fi
info() { printf '%s\n' "${CYAN}==>${RESET} $*" >&2; }
ok()   { printf '%s\n' "${GREEN}✓${RESET} $*" >&2; }
warn() { printf '%s\n' "${YELLOW}!${RESET} $*" >&2; }
die()  { printf '%s\n' "${RED}✗ $*${RESET}" >&2; exit 1; }

# --- locate repo root --------------------------------------------------------
ROOT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
cd "$ROOT_DIR"
ENV_FILE="$ROOT_DIR/.env"

# --- read .env WITHOUT sourcing it -------------------------------------------
env_get() {
    # env_get KEY [default]
    local key="$1" default="${2:-}" line val
    [ -f "$ENV_FILE" ] || { printf '%s' "$default"; return; }
    line="$(grep -E "^${key}=" "$ENV_FILE" | head -n1 || true)"
    [ -n "$line" ] || { printf '%s' "$default"; return; }
    val="${line#*=}"
    val="${val%$'\r'}"
    printf '%s' "$val"
}

# --- compose detection (as an array to stay quote-safe) ----------------------
COMPOSE=()
if docker compose version >/dev/null 2>&1; then
    COMPOSE=(docker compose)
elif command -v docker-compose >/dev/null 2>&1; then
    COMPOSE=(docker-compose)
else
    die "Docker Compose is not installed."
fi

# --- config ------------------------------------------------------------------
PGUSER="$(env_get POSTGRES_USER digitalcore)"
PGDB="$(env_get POSTGRES_DB digitalcore)"
BACKUP_KEY="$(env_get BACKUP_ENCRYPTION_KEY)"
BACKUP_KEEP="${BACKUP_KEEP:-7}"
BACKUP_DIR="$ROOT_DIR/storage/backups"

[ -n "$BACKUP_KEY" ] || die "BACKUP_ENCRYPTION_KEY is empty in .env — cannot create an encrypted backup."
command -v openssl >/dev/null 2>&1 || die "openssl is not installed."

mkdir -p "$BACKUP_DIR"

# --- workspace (auto-cleaned) ------------------------------------------------
TMP="$(mktemp -d)"
trap 'rm -rf "$TMP"' EXIT
PAYLOAD="$TMP/payload"
mkdir -p "$PAYLOAD/storage"

# --- 1. database dump --------------------------------------------------------
info "Dumping database '${PGDB}'…"
if ! "${COMPOSE[@]}" exec -T postgres pg_dump -U "$PGUSER" -d "$PGDB" --clean --if-exists > "$PAYLOAD/db.sql"; then
    die "pg_dump failed (is the postgres service running?)."
fi
ok "Database dumped ($(wc -c < "$PAYLOAD/db.sql") bytes)."

# --- 2. collect .env + storage subdirs (skip missing) ------------------------
if [ -f "$ROOT_DIR/.env" ]; then
    cp -a "$ROOT_DIR/.env" "$PAYLOAD/.env"
else
    warn ".env not found — skipping it in the backup."
fi
for d in receipts exports logs; do
    if [ -d "$ROOT_DIR/storage/$d" ]; then
        cp -a "$ROOT_DIR/storage/$d" "$PAYLOAD/storage/$d"
    else
        warn "storage/$d not found — skipping."
    fi
done

# --- 3. archive + encrypt ----------------------------------------------------
STAMP="$(date +%Y%m%d-%H%M%S)"
ARCHIVE="$TMP/archive.tar.gz"
OUT="$BACKUP_DIR/digitalcore-${STAMP}.tar.gz.enc"

info "Creating archive…"
tar -czf "$ARCHIVE" -C "$PAYLOAD" .

info "Encrypting archive…"
if ! openssl enc -aes-256-cbc -pbkdf2 -salt -pass "pass:$BACKUP_KEY" -in "$ARCHIVE" -out "$OUT"; then
    rm -f "$OUT"
    die "Encryption failed."
fi
chmod 600 "$OUT"
ok "Backup written: $(basename "$OUT") ($(wc -c < "$OUT") bytes, mode 0600)."

# --- 4. prune to newest BACKUP_KEEP ------------------------------------------
mapfile -t all_backups < <(
    find "$BACKUP_DIR" -maxdepth 1 -type f -name '*.tar.gz.enc' -printf '%T@ %p\n' 2>/dev/null \
        | sort -rn | cut -d' ' -f2-
)
if [ "${#all_backups[@]}" -gt "$BACKUP_KEEP" ]; then
    for old in "${all_backups[@]:BACKUP_KEEP}"; do
        rm -f "$old"
        warn "Pruned old backup: $(basename "$old")"
    done
fi
ok "Retention: keeping newest ${BACKUP_KEEP} backup(s)."

# --- result: the final path on the LAST stdout line --------------------------
printf '%s\n' "$OUT"

#!/usr/bin/env bash
# =============================================================================
# DigitalCore — list backups
# =============================================================================
# Lists backup files under storage/backups/ with size, modification date, and
# SHA-256 (read from the .sha256 sidecar when present, otherwise computed).
# Read-only; prints nothing secret.
# =============================================================================
set -euo pipefail

ROOT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
BACKUP_DIR="$ROOT_DIR/storage/backups"

if [ ! -d "$BACKUP_DIR" ]; then
    printf 'No backup directory yet: %s\n' "$BACKUP_DIR"
    exit 0
fi

human() {
    local b="$1"
    if command -v numfmt >/dev/null 2>&1; then
        numfmt --to=iec --suffix=B "$b" 2>/dev/null || printf '%sB' "$b"
    else
        printf '%sB' "$b"
    fi
}

mapfile -t files < <(
    find "$BACKUP_DIR" -type f \
        \( -name '*.tar.gz' -o -name '*.sql.gz' -o -name '*.tar.gz.enc' \) \
        -printf '%T@ %p\n' 2>/dev/null | sort -rn | cut -d' ' -f2-
)

if [ "${#files[@]}" -eq 0 ]; then
    printf 'No backups found in %s\n' "$BACKUP_DIR"
    exit 0
fi

printf '%-46s  %10s  %-19s  %s\n' "FILE" "SIZE" "MODIFIED" "SHA256"
printf '%s\n' "--------------------------------------------------------------------------------"
total=0
for f in "${files[@]}"; do
    size="$(wc -c < "$f")"
    total=$((total + size))
    mtime="$(date -u -d "@$(stat -c %Y "$f")" '+%Y-%m-%d %H:%M:%S' 2>/dev/null || echo '-')"
    sum="-"
    if [ -f "$f.sha256" ]; then
        sum="$(cut -d' ' -f1 < "$f.sha256")"
    elif command -v sha256sum >/dev/null 2>&1; then
        sum="$(sha256sum "$f" | cut -d' ' -f1)"
    fi
    printf '%-46s  %10s  %-19s  %s\n' "$(basename "$f")" "$(human "$size")" "$mtime" "${sum:0:16}…"
done
printf '%s\n' "--------------------------------------------------------------------------------"
printf '%d backup(s), %s total\n' "${#files[@]}" "$(human "$total")"

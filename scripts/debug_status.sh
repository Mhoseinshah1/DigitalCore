#!/usr/bin/env bash
# =============================================================================
# DigitalCore — one-shot debug status snapshot
# =============================================================================
# Prints everything needed to diagnose a broken install WITHOUT leaking secrets:
#   git branch/commit, container status, which .env keys are set (values masked),
#   /health + /ready + /admin results, and recent backend/bot/worker logs.
#
#   bash scripts/debug_status.sh
#
# Secrets are never printed — env values are masked; only key names + set/empty
# state are shown. Safe to paste into an issue.
# =============================================================================
set -Eeuo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
ROOT_DIR="$(cd "$SCRIPT_DIR/.." && pwd)"
cd "$ROOT_DIR"

hr() { printf '%s\n' "──────────────────────────────────────────────────────────"; }
section() { hr; printf '### %s\n' "$*"; hr; }

if docker compose version >/dev/null 2>&1; then
    COMPOSE="docker compose"
elif command -v docker-compose >/dev/null 2>&1; then
    COMPOSE="docker-compose"
else
    COMPOSE=""
fi

API_PORT="$(grep -E '^API_PORT=' .env 2>/dev/null | cut -d= -f2 || true)"
API_PORT="${API_PORT:-8000}"
BASE="http://127.0.0.1:${API_PORT}"

section "Git"
git rev-parse --abbrev-ref HEAD 2>/dev/null | sed 's/^/branch: /' || echo "branch: unknown"
git log --oneline -1 2>/dev/null | sed 's/^/commit: /' || echo "commit: unknown"

section "Containers (${COMPOSE:-no compose} ps -a)"
if [ -n "$COMPOSE" ]; then $COMPOSE ps -a 2>&1 || true; else echo "Docker Compose not found."; fi

section "Environment keys (.env) — values masked, secrets never printed"
if [ -f .env ]; then
    while IFS= read -r line; do
        case "$line" in ''|\#*) continue ;; esac
        key="${line%%=*}"
        val="${line#*=}"
        if [ -z "$val" ]; then
            printf '  %-26s = (empty)\n' "$key"
        else
            printf '  %-26s = (set, %s chars) ****\n' "$key" "${#val}"
        fi
    done < .env
else
    echo "  no .env file found"
fi

section "Health / readiness / admin route"
for path in /health /ready /admin; do
    code="$(curl -s -o /dev/null -w '%{http_code}' "${BASE}${path}" 2>/dev/null || echo 000)"
    printf '  %-8s -> HTTP %s\n' "$path" "$code"
done
printf '  /health body : '; curl -s "${BASE}/health" 2>/dev/null || echo "(unreachable)"; echo
printf '  /ready  body : '; curl -s "${BASE}/ready" 2>/dev/null || echo "(unreachable)"; echo

if [ -n "$COMPOSE" ]; then
    for svc in backend bot worker postgres redis; do
        section "Logs: ${svc} (last 60)"
        $COMPOSE logs "$svc" --tail=60 2>&1 || true
    done
fi

hr
echo "Done. (No secret values were printed.)"

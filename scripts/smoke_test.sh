#!/usr/bin/env bash
# =============================================================================
# DigitalCore — static + runtime smoke test
# =============================================================================
# Runs the full local validation gate and (if Docker is available) an optional
# container startup check. Prints a PASS/FAIL summary and exits non-zero if any
# required check fails. Never prints secrets.
#
#   bash scripts/smoke_test.sh            # static checks (+ docker if present)
#   SKIP_DOCKER=1 bash scripts/smoke_test.sh   # force static-only
# =============================================================================
set -Euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
ROOT_DIR="$(cd "$SCRIPT_DIR/.." && pwd)"
cd "$ROOT_DIR"

PASS=0
FAIL=0
run() {
    local name="$1"; shift
    printf '\n==> %s\n' "$name"
    if "$@"; then printf '    PASS: %s\n' "$name"; PASS=$((PASS + 1));
    else printf '    FAIL: %s\n' "$name"; FAIL=$((FAIL + 1)); fi
}

py() { command -v python >/dev/null 2>&1 && echo python || echo python3; }
PY="$(py)"

run "compileall (app migrations tests)" \
    "$PY" -m compileall -q app migrations tests

run "pytest" \
    "$PY" -m pytest -q

run "bash -n scripts/*.sh + install.sh" \
    bash -c 'for f in scripts/*.sh install.sh; do bash -n "$f" || exit 1; done'

if docker compose version >/dev/null 2>&1; then
    run "docker compose config" docker compose config -q
else
    printf '\n==> docker compose config\n    SKIP: docker not available\n'
fi

# Optional: real container startup check (only when Docker is usable + not skipped).
if [ "${SKIP_DOCKER:-0}" != "1" ] && docker info >/dev/null 2>&1; then
    printf '\n==> optional docker startup check (postgres, redis, backend)\n'
    [ -f .env ] || cp .env.example .env
    if docker compose up -d --build postgres redis backend >/dev/null 2>&1; then
        ok=""
        for _ in $(seq 1 30); do
            if curl -fsS "http://127.0.0.1:8000/health" >/dev/null 2>&1; then ok=1; break; fi
            sleep 2
        done
        if [ -n "$ok" ]; then printf '    PASS: backend /health reachable\n'; PASS=$((PASS + 1));
        else printf '    FAIL: backend /health not reachable\n'; FAIL=$((FAIL + 1)); fi
        docker compose down >/dev/null 2>&1 || true
    else
        printf '    FAIL: docker compose up failed\n'; FAIL=$((FAIL + 1))
    fi
else
    printf '\n==> optional docker startup check\n    SKIP: docker daemon unavailable or SKIP_DOCKER=1\n'
fi

printf '\n──────── smoke summary: %s passed, %s failed ────────\n' "$PASS" "$FAIL"
[ "$FAIL" -eq 0 ]

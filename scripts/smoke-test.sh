#!/usr/bin/env bash
# =============================================================================
# DigitalCore smoke test (Phase 1)
# =============================================================================
# Runs the full happy path and fails loudly if any step fails.
# =============================================================================
set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
ROOT_DIR="$(cd "$SCRIPT_DIR/.." && pwd)"
cd "$ROOT_DIR"

if docker compose version >/dev/null 2>&1; then
    COMPOSE="docker compose"
elif command -v docker-compose >/dev/null 2>&1; then
    COMPOSE="docker-compose"
else
    echo "Docker Compose is not installed." >&2
    exit 1
fi

# Ensure a .env exists so compose can interpolate/env_file.
if [ ! -f .env ]; then
    cp .env.example .env
    echo "Created .env from .env.example for the smoke test."
fi

API_PORT="$(grep -E '^API_PORT=' .env | cut -d= -f2 || true)"
API_PORT="${API_PORT:-8000}"

echo "==> docker compose config"
$COMPOSE config >/dev/null

echo "==> docker compose up -d --build"
$COMPOSE up -d --build

echo "==> alembic upgrade head"
$COMPOSE exec -T backend alembic upgrade head

echo "==> create super admin"
$COMPOSE exec -T backend python scripts/create_admin.py

echo "==> waiting for /health"
for _ in $(seq 1 30); do
    curl -fsS "http://localhost:${API_PORT}/health" >/dev/null 2>&1 && break
    sleep 2
done

echo "==> GET /health"
curl -fsS "http://localhost:${API_PORT}/health"; echo
echo "==> GET /ready"
curl -fsS "http://localhost:${API_PORT}/ready"; echo

echo "SMOKE TEST PASSED"

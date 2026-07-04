# DigitalCore

A Telegram + backend platform. This repository is being built in phases; the
foundation is deliberately minimal, testable, and installable before any product
features are added.

## Phase status

- **Phase 0 — done.** Repository, Docker Compose skeleton, first installer.
- **Phase 1 — done.** Runnable backend with health/readiness, database
  migrations, admin bootstrap, minimal admin auth API, basic bot connectivity,
  installer/management/smoke scripts, and CI.
- **Phase R — done.** Foundation hardening: fix the crypto/config key gap,
  add the Redis client, structured logging, a worker entrypoint, empty service
  packages, runtime `storage/` dirs, and a pytest test baseline. `/ready` now
  checks the database **and** Redis. No behaviour change to existing features.
- **Phase R.1 — done.** Foundation formatting, validation, and installer
  hardening: audited every source/config/script file for readability (all are
  normal multi-line files), hardened `scripts/install.sh` (`set -Eeuo pipefail`
  plus an `ERR` trap that reports the failing line and command and dumps
  `backend`/`bot`/`worker`/`postgres`/`redis` logs on failure — never a silent
  exit), persisted `./storage` into the backend/bot/worker containers, and
  verified the full validation gate (compile, tests, `bash -n`, `docker compose
  config`). No business features added.
- **Phase 2 — planned.** Admin panel, users management, bot database
  registration, role-based permissions, first business modules.

> The Phase 0 Jinja admin panel and business-settings catalog remain in the tree
> but are **dormant** (not wired into the app) because Phase 1 uses an
> email-based admin model. They are rebuilt properly in Phase 2.

## Architecture

| Component | Tech |
|-----------|------|
| Backend API | FastAPI + Uvicorn (service name: `backend`) |
| Bot | aiogram 3 (long polling) |
| Worker | thin async loop (heartbeat; scaffolding) |
| Database | PostgreSQL 16 (async SQLAlchemy 2 + Alembic) |
| Cache | Redis 7 (client wired; `/ready` pings it) |
| Runtime | Docker Compose (single image: `backend` + `bot` + `worker`) |

## Project structure

```
app/
  bot/        Telegram entrypoint (thin) + handlers/
  web/        FastAPI backend + admin panel
  worker/     background worker entrypoint (thin async loop)
  services/   ALL business logic lives here
  schemas/    Pydantic DTOs
  models/     SQLAlchemy ORM models
  xui/        3X-UI integration (only here)
  utils/      small shared helpers
  core/       crypto, security, logging, redis, settings service
  config.py   configuration      database.py  async engine/session
migrations/   Alembic
scripts/      install.sh, manage.sh, smoke-test.sh, create_admin.py, entrypoint.sh
storage/      runtime dirs: receipts/ backups/ exports/ logs/ temp/
tests/        pytest suite
```

Handlers/routes stay thin (parse → call a service → format). Config and the DB
engine live at `app/config.py` and `app/database.py` (not under `app/core/`).

## Requirements

- Ubuntu (installer targets Ubuntu; other Linux works for manual setup)
- Docker Engine and the Docker Compose plugin
- Ports: `8000` (backend) available on the host

## Quick start (Docker)

```bash
git clone https://github.com/Mhoseinshah1/DigitalCore.git digitalcore
cd digitalcore
cp .env.example .env
docker compose up -d --build
docker compose exec backend alembic upgrade head
docker compose exec backend python scripts/create_admin.py
curl -fsS http://localhost:8000/health
curl -fsS http://localhost:8000/ready
```

Expected results:

```json
// GET /health   (liveness — no DB/Redis dependency)
{"status": "ok", "service": "DigitalCore API", "version": "0.1.0"}

// GET /ready    (readiness — 503 if the DB or Redis is down)
{"status": "ready", "database": "ok", "redis": "ok"}
```

## One-command install (production, Ubuntu 22.04/24.04)

On a fresh Ubuntu server (amd64 or arm64), run:

```bash
curl -fsSL https://raw.githubusercontent.com/Mhoseinshah1/DigitalCore/main/scripts/install.sh | sudo bash
```

The installer is standalone: it installs Docker if missing, clones the repo to
`/opt/digitalcore`, **generates all secrets** (`POSTGRES_PASSWORD`, `SECRET_KEY`,
`JWT_SECRET`, `FERNET_KEY`, `BACKUP_ENCRYPTION_KEY`, `DATABASE_URL`, `REDIS_URL`,
`WEB_PANEL_URL`), brings the stack up, runs `alembic upgrade head`, creates the
admin, and gates on `/health` + `/ready`. It never reports success unless the app
is actually healthy. It asks **only** for `BOT_TOKEN`, `MAIN_ADMIN_TELEGRAM_ID`,
`DOMAIN`, the admin **username** (default `admin`), and an optional web-admin
password — everything else is configured later from the panel. At the end it
prints a full installation summary (panel URL, login page, admin username and
password, bot status, management commands) — secret keys are never printed;
they live in `/opt/digitalcore/.env` (mode `0600`) — back it up.

The panel admin signs in with the **username** (the optional `ADMIN_EMAIL`, when
set, also works as the login identifier).

Fully non-interactive (CI/automation):

```bash
curl -fsSL .../scripts/install.sh | sudo BOT_TOKEN=123:abc MAIN_ADMIN_TELEGRAM_ID=111 \
    DOMAIN=panel.example.com ADMIN_USERNAME=admin NON_INTERACTIVE=1 bash
```

> **The panel is served over plain HTTP on port `:8000`.** For a real deployment,
> put **Nginx (or another reverse proxy) with HTTPS/TLS in front of it** — e.g.
> terminate TLS at Nginx for your `DOMAIN` and proxy to `127.0.0.1:8000`. TLS is
> not configured by the installer.

Or install from a clone (the root `./install.sh` forwards to `scripts/install.sh`):

```bash
git clone https://github.com/Mhoseinshah1/DigitalCore.git digitalcore
cd digitalcore
sudo ./install.sh
```

Re-running the installer is safe — it keeps your existing `.env` and secrets.

## Local development (without Docker)

```bash
python -m venv .venv && . .venv/bin/activate
pip install -r requirements.txt
cp .env.example .env
# point DATABASE_URL at a reachable Postgres (or use the compose one)
alembic upgrade head
python scripts/create_admin.py
uvicorn app.web.main:app --reload --port 8000
```

## Tests

```bash
pip install -r requirements-dev.txt
python -m pytest -q
```

Tests use an in-memory SQLite database by default; set `TEST_DATABASE_URL` to a
Postgres DSN to run against Postgres instead. The full local check is:

```bash
python -m compileall app migrations tests
python -m pytest -q
bash -n scripts/*.sh
docker compose config
```

The background worker can be run directly:

```bash
python -m app.worker.main    # logs a heartbeat every ~30s; Ctrl-C to stop
```

## Common commands

| Action | Command |
|--------|---------|
| Migrate | `docker compose exec backend alembic upgrade head` |
| Create admin | `docker compose exec backend python scripts/create_admin.py` |
| Reset admin password | `docker compose exec backend python scripts/create_admin.py --reset-password` |
| Status | `bash scripts/manage.sh status` |
| Logs (no follow) | `bash scripts/manage.sh logs backend` |
| Logs (follow) | `bash scripts/manage.sh logs backend --follow` |
| Restart | `bash scripts/manage.sh restart` |
| Stop | `bash scripts/manage.sh down` |
| Health | `bash scripts/manage.sh health` |
| Smoke test | `bash scripts/smoke-test.sh` |

## Operations (backup, update, restore)

The operational scripts read config from `.env` (never sourced), auto-detect
`docker compose` / `docker-compose`, print colored status, and never log secrets.

**Apply a new phase / update to the latest code** — push to `main`, then on the
server run:

```bash
cd /opt/digitalcore && sudo bash scripts/update.sh
```

`update.sh` takes an encrypted backup first, pulls `origin/$REPO_BRANCH` (default
`main`), rebuilds, runs `alembic upgrade head`, and gates on `/health` + `/ready`.
If the build, migration, or health check fails, it automatically rolls the **code**
back to the previous commit and rebuilds. A **DB** rollback (if a migration changed
the schema) is manual by design — it prints the exact `restore.sh … --yes` command
using the backup it just made.

| Action | Command |
|--------|---------|
| Encrypted backup | `sudo bash scripts/backup.sh` (or `manage.sh backup`) |
| Restore latest backup | `sudo bash scripts/restore.sh --latest` (add `--yes` to skip the prompt) |
| Restore a specific backup | `sudo bash scripts/restore.sh storage/backups/<file>.tar.gz.enc` |
| Safe update + rollback | `sudo bash scripts/update.sh` (or `manage.sh update`) |
| Health check | `bash scripts/healthcheck.sh` (or `manage.sh health`) |

Backups are AES-256 encrypted with `BACKUP_ENCRYPTION_KEY` and written to
`storage/backups/` (mode `0600`); the newest `BACKUP_KEEP` (default 7) are kept.
**Restore is destructive** — it overwrites the database, so it requires a typed
`yes` unless `--yes` is passed.

## Admin auth API

```http
POST /api/auth/login    { "email": "...", "password": "..." }  -> { "access_token", "token_type" }
GET  /api/auth/me       Authorization: Bearer <token>          -> admin profile
```

## Database

Migrations are explicit (`op.create_table`, not `create_all`). Phase 1 tables:
`admins` (email/password), `users` (telegram user, nullable for now), `settings`
(key/value/is_secret). The backend does **not** auto-migrate; run migrations
explicitly as shown above.

## Environment

`.env` is gitignored; `.env.example` is safe to commit. Copy and edit it:

```bash
cp .env.example .env
```

The backend runs fine with `TELEGRAM_BOT_TOKEN` empty. The bot service logs a
clear message and exits cleanly when the token is missing.

## Troubleshooting

- **`scripts/install.sh: No such file or directory`** — run from the repository
  root after cloning: `cd digitalcore && sudo bash scripts/install.sh`. Make sure
  the clone completed and you are in the project directory.
- **Docker not installed / daemon not running** — install Docker Engine +
  Compose plugin (https://docs.docker.com/engine/install/ubuntu/) and ensure the
  daemon is running (`sudo systemctl start docker`); run the installer with
  `sudo` if your user is not in the `docker` group.
- **Backend health failed** — inspect containers and logs:
  `docker ps -a` and `docker compose logs backend --tail=200`. A common cause is
  the port `8000` already being in use.
- **Not ready (`/ready` returns 503)** — `/ready` checks both Postgres and Redis.
  The JSON body shows which is down (`"database"`/`"redis": "error"`). Postgres or
  Redis may still be starting; wait and retry. Check `docker compose logs postgres`
  / `docker compose logs redis` and confirm `DATABASE_URL` / `REDIS_URL` in `.env`.
- **Telegram token missing** — expected in Phase 1. The backend is unaffected;
  the `bot` container exits cleanly (code 0) and will not restart-loop. Set
  `TELEGRAM_BOT_TOKEN` in `.env` and `docker compose up -d bot` to enable it.

See **Project structure** near the top of this document for the directory layout.

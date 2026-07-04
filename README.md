# DigitalCore

A Telegram + backend platform. This repository is being built in phases; the
foundation is deliberately minimal, testable, and installable before any product
features are added.

## Phase status

- **Phase 0 — done.** Repository, Docker Compose skeleton, first installer.
- **Phase 1 — current.** Runnable backend with health/readiness, database
  migrations, admin bootstrap, minimal admin auth API, basic bot connectivity,
  installer/management/smoke scripts, and CI.
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
| Database | PostgreSQL 16 (async SQLAlchemy 2 + Alembic) |
| Cache | Redis 7 (present; not required by Phase 1 code) |
| Runtime | Docker Compose (single image: `backend` + `bot`) |

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
// GET /health
{"status": "ok", "service": "DigitalCore API", "version": "0.1.0"}

// GET /ready
{"status": "ready", "database": "ok"}
```

## Installer v0

The installer performs the whole flow and refuses to report success unless
`/health` and `/ready` both pass:

```bash
sudo bash scripts/install.sh
```

It checks Ubuntu/Docker/Compose, creates `.env` from `.env.example` (prompting
for domain, admin email/password, and optional Telegram token/admin id), builds
and starts the stack, runs migrations, creates the super admin, and verifies
health/readiness. On failure it prints `docker ps -a` and
`docker compose logs backend --tail=200` and exits non-zero.

`./install.sh` from the repo root forwards to `scripts/install.sh`.

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
- **Database not ready (`/ready` returns 503)** — Postgres may still be starting;
  wait and retry. Check `docker compose logs postgres` and confirm `DATABASE_URL`
  in `.env` matches the `postgres` service credentials.
- **Telegram token missing** — expected in Phase 1. The backend is unaffected;
  the `bot` container exits cleanly (code 0) and will not restart-loop. Set
  `TELEGRAM_BOT_TOKEN` in `.env` and `docker compose up -d bot` to enable it.

## Layout

```
docker-compose.yml         postgres, redis, backend, bot
Dockerfile                 single image for backend + bot
.env.example               environment template (safe to commit)
app/
  config.py                settings loader
  database.py              async SQLAlchemy engine/session
  models/                  Admin, User, Setting
  core/security.py         bcrypt + JWT
  web/main.py              FastAPI app: /health, /ready
  web/api/auth.py          /api/auth/login, /api/auth/me
  bot/main.py              aiogram bot: /start, /ping
migrations/                Alembic (0001_initial: admins, users, settings)
scripts/
  install.sh               installer v0
  manage.sh                status/logs/restart/down/health
  smoke-test.sh            full happy-path check
  create_admin.py          super-admin bootstrap
  entrypoint.sh            container role selector
.github/workflows/ci.yml   syntax + compose config checks
```

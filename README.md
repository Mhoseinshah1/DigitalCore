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
- **Phase 2 — done.** Full admin foundation: a Persian **RTL admin panel** under
  `/admin` with a right sidebar (grouped, nested sections + active highlighting),
  admin login, five RBAC roles with permission helpers, **users management**
  (list/detail, block/unblock, verify, admin note, **wallet balance adjustment**
  with an audited ledger), **settings pages** (general / telegram / payment /
  bot messages), **product management foundation**, **audit logs**, bot user
  registration on `/start` (with blocked-user and maintenance protection), and a
  Telegram admin panel (`/admin_stats`, `/admin_users`, `/admin_settings`,
  block/unblock/add/subtract balance by Telegram ID). Purchase flow, receipt
  approval, license delivery, and 3X-UI provisioning are intentionally NOT
  included — they are later phases.
- **Phase 2.1 — done.** V2Ray / 3X-UI **server-binding foundation**: manage
  multiple 3X-UI panels and their inbounds from the admin panel, and bind every
  **V2Ray** product to exactly one server + one inbound (license products carry
  no binding). Server passwords and API tokens are stored **Fernet-encrypted**,
  never rendered, and never written to audit metadata; an empty password/token
  on edit keeps the stored one. The product form is **type-aware** (license
  hides the XUI fields; V2Ray shows server + inbound dropdowns fed by a JSON
  endpoint), and the bot's V2Ray detail view shows duration/traffic/IP-limit and
  an optional friendly server name only. Creating clients in 3X-UI, payment
  approval, provisioning, subscription/QR links, and traffic sync jobs are still
  NOT included — later phases.

### Phase 2.1 — V2Ray / 3X-UI binding

A new **“V2Ray / 3X-UI”** sidebar group (permission `manage_xui`) manages panel
servers and their inbounds so V2Ray products can be bound to a concrete
server+inbound. Credentials are encrypted at rest (`app/core/crypto.py`); the
server list/edit pages and audit logs never reveal them. Live **test /
sync-inbounds** are best-effort and never block CRUD or product binding.

| Area | Routes |
|------|--------|
| Servers | `GET /admin/xui-servers`, `/admin/xui-servers/create`, `/admin/xui-servers/{id}/edit`, `POST …/{id}/{deactivate,test,sync-inbounds}` |
| Inbounds | `GET /admin/xui-servers/{id}/inbounds`, `…/inbounds/create`, `GET /admin/xui-inbounds/{id}/edit`, `POST /admin/xui-inbounds/{id}/{edit,deactivate}`, `GET /admin/xui-inbounds` (overview) |
| Product binding | `GET /admin/api/xui-servers/{id}/inbounds` (JSON, active only) feeds the type-aware product form |

Validation (`app/services/product_service.py`): a `license` product must not set
a server/inbound; a `v2ray` product **requires** duration, traffic, a server and
an inbound; the inbound must belong to the chosen server; and an **active**
V2Ray product needs an active server and inbound. The legacy `/admin/servers`
path now `301`-redirects to `/admin/xui-servers`. Audited actions:
`xui_server_created/updated/deactivated/tested`, `xui_inbounds_synced`,
`xui_inbound_created/updated/deactivated`, `product_bound_to_xui`.

- **Phase 3 — done.** **Orders & card-to-card receipt flow.** From the bot a user
  opens the product list, taps **Buy**, and an **order** (`pending_payment`) plus a
  **payment** (`pending`) are created; the bot then shows the card-to-card details
  from **Settings → Payment** (`card_number` / `card_owner` / `sheba_number` /
  `payment_instructions`). The user uploads a receipt (jpg/jpeg/png/webp/pdf,
  ≤ 10 MB), it is stored safely on disk and the order moves to `waiting_admin`,
  the payment to `receipt_submitted`. Admins see the pending queue in the panel
  and get a Telegram notification (with the receipt). **Not** in this phase:
  approval/rejection, license delivery, V2Ray client creation, subscription/QR
  links, wallet payments, coupons/referrals/tickets/reports/gateway — those are
  Phase 4+.
- **Phase 4 — done.** **Approval / delivery + receipt-review admin quick actions.**
  An admin reviewing a submitted receipt — in the web panel **or** in Telegram —
  can **approve** (which delivers: a license code is popped from the product's key
  pool, or a V2Ray client is provisioned on the bound server/inbound) or **reject
  with a reason**, and can run user-management actions in the same place: **add /
  subtract wallet balance**, **block**, **restrict** (softer than a block), and
  **view user**. Restricted users can still `/start` and read rules/support but
  cannot order, buy, or submit receipts. **Not** in this phase: subscription/QR
  link generation, wallet *purchase*/top-up, gateways, coupons/referrals/tickets.

### Phase 4 — approval, delivery & receipt-review quick actions

**Approve / reject** (`app/services/payment_service.py`): a submitted receipt
(order `waiting_admin`, payment `receipt_submitted`) can be approved or rejected
**once** — the state guard blocks duplicates. Approval sets the order/payment to
`approved` (+`paid_at`/`approved_at`/`admin_id`) then triggers delivery; rejection
sets `rejected` with the required reason.

**Delivery** (`app/services/delivery_service.py`, best-effort, never un-approves):
- *license* → pops the next code from the product's **key pool**
  (`app/services/license_service.py`, `license_keys` table) into
  `order.delivered_payload` and marks the order `delivered`. Empty pool → the
  order stays `approved` and the admin is told to stock keys.
- *v2ray* → provisions a client on the bound 3X-UI server/inbound via
  `xui_service.add_client`; on failure the order stays `approved`.

**Receipt-review quick actions** — permission-gated, from `/admin/orders/{id}`,
the pending-receipts list, and (mirrored) `/admin/users/{id}`:

| Action | Web route | Telegram button | Permission |
|--------|-----------|-----------------|------------|
| Approve / Reject | `POST …/{id}/{approve,reject}` | ✅ / ❌ | `process_payments` |
| Add / Subtract balance | `POST …/{id}/{add,subtract}-balance` | 💰 / ➖ (FSM: amount → reason) | `adjust_wallet` |
| Block user | `POST …/{id}/block-user` | 🚫 (confirm) | `block_users` |
| Restrict / Unrestrict | `POST …/{id}/{restrict,unrestrict}-user` | ⚠️ (FSM: reason) | `restrict_users` |
| View user | link to `/admin/users/{id}` | 👤 | `view_users` |

Wallet moves go through `app/services/wallet_service.py` (records
`balance_before`/`balance_after`/`type`/`admin_id`/`reason`; refuses to go
negative unless `allow_negative_wallet` is on; reason required). The Telegram FSM
carries `order_id`/`user_id`/`admin_id`/`action`, only the **initiating admin**
can complete it, `/cancel` (or the لغو button) aborts, and non-admin callbacks are
refused. Restriction is enforced by `RestrictedMiddleware`.

**Roles** (`app/core/permissions.py`): owner → all; admin → approve/reject +
wallet + block + restrict; accountant → approve/reject + wallet (no block/restrict);
support → block + restrict (no approve, no wallet); viewer → read-only. Audited
actions: `payment_approved`, `payment_rejected`, `order_delivered`,
`admin_wallet_added_from_receipt_review`,
`admin_wallet_subtracted_from_receipt_review`, `user_blocked_from_receipt_review`,
`user_restricted_from_receipt_review`, `user_unrestricted` (no secrets in metadata).

### Phase 3 — orders & card-to-card receipts

**Bot flow** (`app/bot/handlers/user/{products,orders}.py`): Buy → `create_order`
+ `create_payment_for_order` → card-to-card instructions → the user sends the
receipt as a photo/document → it is downloaded, validated, stored, and the order
enters the admin review queue. A robust FSM (`waiting_for_receipt`) plus a
stateless fallback means a receipt is matched to the user's latest pending order
even without an active flow; wrong file types, oversize files, and text-instead-of-file
each get a clear message. `/orders` (My Orders) lists the user's orders with Persian
statuses.

**Receipt storage.** Files live at
`storage/receipts/YYYY/MM/<order_number>_<safe_filename>` (the `./storage` volume
is mounted into the containers, so receipts persist). Only the **relative path** +
metadata (mime, original name, size, Telegram file_id) are stored in `payments`;
the bytes never touch the DB or audit log. Filenames are sanitised and the serving
route re-validates containment to defeat path traversal.

**Admin panel** — a new **Orders** sidebar group (permission `view_payments`):

| Area | Routes |
|------|--------|
| Orders | `GET /admin/orders` (all), `GET /admin/orders/pending-receipts` (waiting_admin only), `GET /admin/orders/{id}` (detail + timeline + related audit) |
| Receipts | `GET /admin/receipts/{payment_id}` — admin-only, inline, `nosniff` + `no-store`, path-traversal-safe |

Order/payment state machines are stored in full but only `pending_payment →
waiting_admin → cancelled` and `pending → receipt_submitted` are exercised;
approval/rejection/delivery buttons are placeholders. Persian/English status labels
come from `app/core/statuses.py`. Audited actions: `order_created`,
`payment_created`, `receipt_submitted`, `order_cancelled`, `admin_viewed_receipt`.

### Phase 2 admin panel

The panel is served at **`/admin`** (login at `/admin/login`; `/` and `/login`
redirect there). The right RTL sidebar groups: Dashboard · Users (all / blocked
/ wallet adjustments) · Products · Payments (payment settings) · Bot settings
(messages) · System settings (general / telegram / maintenance / sales) · Logs
(audit) · and clearly-labelled **“coming soon”** placeholders for orders,
licenses, V2Ray services, 3X-UI servers, tickets, coupons, referrals, backups,
and reports.

| Area | Routes |
|------|--------|
| Auth | `GET/POST /admin/login`, `GET /admin/logout` |
| Dashboard | `GET /admin` |
| Users | `GET /admin/users`, `/admin/users/blocked`, `/admin/users/wallet`, `/admin/users/{id}`, `POST /admin/users/{id}/{block,unblock,verify,note,wallet-adjust}` |
| Settings | `GET/POST /admin/settings/{general,telegram,payment,bot-texts}` |
| Products | `GET /admin/products`, `/admin/products/create`, `/admin/products/{id}/edit`, `POST …/toggle-active`, `…/delete-or-hide` |
| Audit | `GET /admin/audit-logs` |

**Roles & permissions** (`app/core/permissions.py`): `owner` (all), `admin`
(users/settings/products/wallet/payments/logs), `support` (view + block users),
`accountant` (view users, adjust wallet, view payments), `viewer` (read-only
dashboard + users). Helpers: `is_owner`, `can_view_dashboard`,
`can_manage_users`, `can_adjust_wallet`, `can_manage_settings`,
`can_manage_products`, `can_view_payments`, `can_view_logs`, `can_manage_admins`.

Every sensitive action (login, block/unblock, wallet adjust, settings change,
product create/update/toggle) writes an **audit log** row.

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

Or run all of the above (plus an optional container startup check when Docker is
available) with one command:

```bash
bash scripts/smoke_test.sh
```

> A regression test (`tests/test_foundation_install.py`) asserts that every
> runtime import under `app/` is declared in `requirements.txt` — this guards
> against a dependency being only in `requirements-dev.txt`, which would pass the
> tests but crash the production container on startup.

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

Migrations are explicit (`op.create_table`, not `create_all`); the backend does
**not** auto-migrate — run migrations explicitly as shown above. Core tables:
`admins`, `users` (telegram profile + `wallet_balance`, `is_blocked`,
`is_verified`, `admin_note`, `language_code`), `settings` (key/value/is_secret),
`products`, `audit_logs` (+ `meta`/`ip_address`), `wallet_transactions` (signed
ledger), the 3X-UI tables, and the `orders`/`payments` tables. Phase 2 adds
migration **0008** (user/audit columns, `wallet_transactions`, settings-key
reconciliation); Phase 2.1 adds migration **0009**
(`xui_servers.is_active/last_error`, nullable `username`/`encrypted_password`,
`xui_inbounds.network/security`, and the `products.xui_server_id/xui_inbound_id`
FK bindings); Phase 3 adds migration **0010** (the `orders` and `payments`
tables); Phase 4 adds migration **0011** (`users.is_restricted/restriction_reason/
restricted_until`, `wallet_transactions.balance_before/type`,
`orders.delivered_payload`, and the `license_keys` pool table). All run cleanly on
a fresh **and** an existing database and preserve operator-entered values.

Run `python -m app.seed` once after migrating to insert the default settings
rows (idempotent; never overwrites custom values).

## Phase 3 manual test checklist

1. Log in to the admin panel and open **Settings → Payment**; set the card
   number, card owner, SHEBA, and payment instructions.
2. Under **Products**, create an active **license** product (and, if XUI servers
   exist, an active **V2Ray** product bound to a server + inbound).
3. In the bot: `/start` → open **Products** → pick a product → tap **Buy**.
4. Confirm the order is created and the card-to-card instructions appear.
5. Send a receipt photo/document; confirm the *“receipt recorded, awaiting admin
   review”* reply.
6. In the panel, open **Orders → Pending receipts** and confirm the order shows
   up as `waiting_admin` with the receipt link.
7. Open the receipt via `/admin/receipts/{id}` — it must only load while logged
   in as an admin.
8. In the bot, `/orders` (My Orders) shows the order with its Persian status.
9. Confirm the order is `waiting_admin` (Phase 4 approves/delivers it).

## Phase 4 manual test checklist

1. Stock license keys for a license product (via `license_service.add_keys`).
2. As a user, order the product and submit a receipt (Phase 3 flow).
3. In **Orders → Pending receipts** (or the Telegram notification), open the receipt.
4. Click **Add balance**, enter an amount + reason → the user's wallet increases,
   a wallet transaction appears, and an audit row is written.
5. Click **Block user** → the user can no longer use the bot; **unblock** from the
   user detail page.
6. Click **Restrict user** with a reason → the user can `/start` but cannot create
   a new order or submit a receipt; **unrestrict** to reverse it.
7. Click **Approve** → the order becomes `delivered` and the license code shows in
   the order detail (`delivered_payload`). For a V2Ray product a client is
   provisioned on the bound server/inbound.
8. Reject a different receipt with a reason; confirm duplicate approve/reject is
   blocked.
9. Repeat the same actions from the **Telegram** notification buttons (approve,
   reject, add/subtract balance, block, restrict, view user) — confirm only the
   admin who started an FSM action can finish it, and `/cancel` aborts.

## Environment

`.env` is gitignored; `.env.example` is safe to commit. Copy and edit it:

```bash
cp .env.example .env
```

The backend runs fine with `TELEGRAM_BOT_TOKEN` empty. The bot service logs a
clear message and exits cleanly when the token is missing.

The bot token and admin id accept either the canonical or the short name, so a
fresh install never breaks over a variable-name choice:

| Canonical (written by the installer) | Also accepted (alias) |
|--------------------------------------|-----------------------|
| `TELEGRAM_BOT_TOKEN`                  | `BOT_TOKEN`            |
| `TELEGRAM_ADMIN_ID`                   | `MAIN_ADMIN_TELEGRAM_ID` |

The canonical name wins if both are set.

## Troubleshooting

- **First step for any broken install** — capture a full, secret-safe snapshot
  (git commit, container status, which `.env` keys are set with values masked,
  `/health` + `/ready` + `/admin` results, and recent backend/bot/worker logs):

  ```bash
  cd /opt/digitalcore && bash scripts/debug_status.sh
  ```

- **Panel / bot container exits immediately on a fresh install** — check the
  logs (`docker compose logs backend`, `docker compose logs bot`). A
  `ModuleNotFoundError` means a runtime import isn't declared in
  `requirements.txt` (it must not live only in `requirements-dev.txt`). This is
  guarded by `tests/test_foundation_install.py` and `scripts/smoke_test.sh`.
- **Where is the panel?** — the admin panel is served at `/admin` (login at
  `/admin/login`; it redirects there when signed out). `/` and `/login` redirect
  to the panel for convenience.
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

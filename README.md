# DigitalCore

A Telegram + web-panel platform for selling digital products (V2Ray subscriptions,
license keys) with card-to-card payments and a wallet.

This repository currently contains the **foundation**: a minimal one-command
installer, the boot/business settings split, automatic secret generation, owner
admin + default settings seeding, and an admin web panel with a **Settings** page.
Business logic (sales flows, V2Ray/3X-UI integration, licenses, payments) is added
in later phases and is configured from the panel — never from the installer.

## Design rule

> The installer only **boots** the platform. The admin panel **configures** the
> business.

The installer asks for the four things required for first boot and nothing else:

1. Telegram `BOT_TOKEN`
2. Main admin Telegram ID
3. Web panel domain
4. Web admin password (optional — generated if left blank)

It never asks for card numbers, SHEBA, card owner, log group, force-join channel,
products, V2Ray plans, 3X-UI servers, license stock, payment/support texts,
tutorials, or any other business setting. All of those are configured later from
**Settings** in the admin panel.

## Requirements

- Docker and Docker Compose
- A domain (or IP) pointing at the host for the web panel

## Install

```bash
git clone <this-repo> digitalcore
cd digitalcore
./install.sh
```

The installer will:

- check Docker is available,
- ask the four questions above,
- generate every secret automatically (`SECRET_KEY`, `JWT_SECRET`, `FERNET_KEY`,
  `BACKUP_ENCRYPTION_KEY`, database password),
- default `ADMIN_TELEGRAM_IDS` to your main admin ID,
- derive `WEB_PANEL_URL` from the domain,
- write `.env`,
- build and start the stack,
- and on first boot create the **owner admin** (your Telegram ID) plus **empty
  default records** for every business setting.

At the end it prints the panel URL and login. If you left the password blank, the
generated password is shown **once** — save it.

### Non-interactive install

```bash
BOT_TOKEN=123:abc \
MAIN_ADMIN_TELEGRAM_ID=123456789 \
WEB_PANEL_DOMAIN=panel.example.com \
WEB_ADMIN_PASSWORD=optional \
./install.sh --non-interactive
```

## After install

Open the panel, sign in as `admin` (or your Telegram ID) and go to **Settings** to
configure:

| Section   | What you configure                                                        |
|-----------|---------------------------------------------------------------------------|
| Payment   | Card number, SHEBA, card owner, payment instructions                      |
| Telegram  | Log group/channel, force-join channel, support username, broadcasts       |
| Bot texts | Start, rules, payment, successful/rejected payment, expiration warning    |
| Business  | Enable sales / card payment / wallet / free test, min wallet top-up       |
| V2Ray     | Default inbound (server management & inbound sync come in a later phase)   |
| License   | Low-stock alert threshold (products & stock import come in a later phase)  |

## Configuration model

`.env.example` is split into two clearly separated sections:

- **Boot settings** — required to start (bot token, admin ID, domain, datastore
  URLs, secrets, maintenance flag). Filled in by the installer.
- **Business settings** — optional, empty by default, configured from the panel.
  A handful can be pre-seeded from the environment for automated deployments, but
  the installer never asks for them.

The exact settings records seeded on first boot (all empty/default) include:
`log_group_id`, `force_join_channel`, `default_card_number`, `default_card_owner`,
`default_sheba`, `payment_text`, `start_text`, `rules_text`, `support_text`,
`maintenance_mode`, plus the rest of the catalog in
[`app/core/defaults.py`](app/core/defaults.py).

## Architecture

| Component | Tech                                        |
|-----------|---------------------------------------------|
| Web panel | FastAPI + Jinja2 (server-rendered) + JWT    |
| Bot       | aiogram 3 (long polling)                     |
| Database  | PostgreSQL 16 (async SQLAlchemy + Alembic)  |
| Cache     | Redis 7                                      |
| Runtime   | Docker Compose (single image, `web` + `bot`) |

Secrets flagged as secret in the settings catalog are encrypted at rest with
`FERNET_KEY`.

## Common commands

```bash
make up        # start
make down      # stop
make logs      # tail logs
make ps        # status
make seed      # re-run idempotent seeding
make migrate   # apply migrations
```

## Layout

```
install.sh              one-command installer (boot only)
.env.example            boot vs business settings, documented
docker-compose.yml      postgres, redis, web, bot
Dockerfile              single image for web + bot
app/
  config.py             boot settings from env
  core/
    defaults.py         canonical business-settings catalog
    settings_service.py typed get/set with encryption
    crypto.py           Fernet encryption of secret settings
    security.py         password hashing + JWT
  models/               Admin, Setting
  seed.py               owner admin + default settings (idempotent)
  web/                  FastAPI panel: auth, Settings page, JSON API
  bot/                  aiogram skeleton
migrations/             Alembic
```

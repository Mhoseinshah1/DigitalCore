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
  can **approve** (which hands the order to the delivery dispatcher — see Phase 5
  for license delivery and Phase 6 for V2Ray provisioning) or **reject
  with a reason**, and can run user-management actions in the same place: **add /
  subtract wallet balance**, **block**, **restrict** (softer than a block), and
  **view user**. Restricted users can still `/start` and read rules/support but
  cannot order, buy, or submit receipts. **Not** in this phase: subscription/QR
  link generation, wallet *purchase*/top-up, gateways, coupons/referrals/tickets.
- **Phase 5 — done.** **License stock + real license delivery.** License products
  are now fully functional: admins import email/password licenses (parsed text /
  CSV / file) into a per-product stock, and when a license order is approved the
  dispatcher **reserves and sells exactly one** license (row-locked so it is never
  sold twice), attaches it to the order + buyer, sends the credentials to the user
  in Telegram, and marks the order `delivered`. Users see their licenses with
  `/my_licenses`; admins get stock/sold/low-stock pages, a detail page (password
  gated by permission), and redeliver/replace/block/mark-broken actions.
- **Phase 6 — done.** **Real 3X-UI V2Ray provisioning.** Approving a paid V2Ray
  order now creates a client on the bound 3X-UI server + inbound, **verifies it
  after writing**, stores a local `V2RayService`, and delivers the subscription
  link + QR to the buyer; the order becomes `delivered`. Provisioning is
  **idempotent** (deterministic client email + unique `order_id` + a held order
  lock) so an order never gets two clients, and it **retries safely** — a failure
  keeps the approval, flags the order with a `delivery_error`, and lets an admin
  retry. Users see `/my_services`; admins get `/admin/v2ray-services`
  (list/detail + refresh-usage / disable / enable / delete / reset-traffic /
  retry). **Not** in this phase: renewal / add-traffic, wallet purchase,
  coupons / referrals / tickets / reports, Marzban / Hiddify adapters.
- **Phase 7 — done.** **Wallet top-up + wallet payment.** The wallet is now a
  real payment method. Users top up by card-to-card (amount → receipt → admin
  review → credit), pay for orders straight from their balance (which runs the
  same delivery dispatcher, so a license/V2Ray order is fulfilled immediately),
  and see `/wallet` + history. Every balance change **locks the user row** and
  writes a `WalletTransaction` (balance_before/after); top-up approve, wallet
  charge, and refund are each **idempotent** (proven under real Postgres
  concurrency — never double-credit, double-charge, or double-refund). Admins
  get `/admin/wallet/topups` (+ pending, detail, approve/reject with receipt
  view), `/admin/wallet/transactions`, Telegram approve/reject buttons, and a
  **Refund to wallet** action on the order page. **Not** in this phase: online
  payment gateway, coupons / referrals / tickets / reports, reseller.
- **Phase 8 — done.** **Renewal + add-traffic + V2Ray service lifecycle.** A
  V2Ray service is now a living thing: users **renew** (extend the expiry from
  `max(now, current expiry)` by a plan's duration) and **add traffic** (grow the
  quota) from `/my_services`, paying by card-to-card or wallet like any order.
  Delivery for a renew/add-traffic order runs under the **order-row lock** with a
  single terminal commit, so it is **idempotent** — a redelivery never
  double-renews (proven under real Postgres concurrency) — and a panel failure
  **never** rolls back the payment (the order stays retryable). A background
  worker sweep marks services **expired** / **over-quota**, refreshes usage from
  3X-UI on an interval (batched, never spammy), optionally **auto-disables**
  expired / over-quota clients on the panel, and sends **one-shot** expiry /
  traffic warnings. Admins get renew / add-traffic controls + live
  remaining-traffic / remaining-days on the service page. **Not** in this phase:
  coupons / referrals / tickets / reseller / reports, online payment gateway,
  Marzban / Hiddify adapters.
- **Phase 9 — done.** **Support tickets + tutorials / knowledge base.** Users open
  **support tickets** from `/support` (subject → message → optional photo/file),
  browse `/tickets`, reply, close, and reopen; staff manage them from
  `/admin/tickets` (reply with attachment, close, assign-to-self, set priority)
  and from Telegram (`/admin_tickets`, `/admin_ticket <number>`). Every admin
  reply pings the user. **Attachments** are validated (safe types, ≤ configurable
  MB) and stored on disk under `storage/tickets/YYYY/MM/`; the serving route is
  auth-gated and path-traversal-guarded — bytes never touch the DB or audit log.
  A **tutorials / knowledge base** lets admins author categories + articles
  (plain text, HTML-escaped at render) tagged by platform / product type; users
  read them from `/tutorials`, and a **connection-guide button** appears under a
  V2Ray delivery. **Not** in this phase: coupons, referrals, reseller, reports,
  online payment gateway, Marzban / Hiddify adapters, backup changes.
- **Phase 10 — done.** **Coupons + referrals.** **Discount coupons** (percent or
  fixed, with caps, min-order, per-coupon + per-user limits, date windows, and
  product / product-type / action restrictions) are applied in the bot buy flow:
  the user is asked *"کد تخفیف دارید؟"*, and a valid code discounts `final_amount`
  before the payment-method picker — wallet charges and card-to-card instructions
  both show the discounted total. Codes are normalized (UPPERCASE) and validated
  server-side; a coupon is **consumed** (used_count bumped, `CouponUsage` written)
  only when the order is paid, under a coupon row lock and guarded by a unique
  `(coupon_id, order_id)` — no double-usage (proven under Postgres concurrency).
  **Referrals**: each user gets a `t.me/<bot>?start=ref_<code>` link (`/referral`
  shows it + stats); the first `/start ref_<code>` attaches the referrer (never
  self, never overwritten). When a referred user's first delivered order clears
  the minimum, a **reward** is minted for the referrer — auto-paid to their wallet
  or left `pending` for admin approval per settings, idempotent via a unique
  `order_id` + row lock (no double reward, proven under Postgres concurrency).
  Admins get **Marketing** pages: `/admin/coupons` (CRUD + usages),
  `/admin/referrals`, and `/admin/referral-rewards` (approve / pay / reject).
  **Not** in this phase: reseller, reports dashboard, online payment gateway,
  Marzban / Hiddify adapters, backup changes.
- **Phase 11 — done.** **Reports + analytics + CSV exports.** A read-only
  **reports** area (`/admin/reports/...`) turns the existing data into admin
  analytics: an **overview** of revenue / delivered orders / new + active users
  and a "needs attention" panel, plus **sales, orders, payments, wallet,
  products, users, licenses, V2Ray, marketing and support** report pages, each
  with today / yesterday / 7-day / 30-day / this-month / last-month / custom
  date filters. All figures come from `report_service` **SQL aggregation**
  (`func.count/sum/date`, `group_by`) — no full-table scans into Python. Six
  admin-only **JSON endpoints** (`/admin/reports/api/...`) feed chart-ready data,
  and ten **CSV exports** (`/admin/reports/export/*.csv`, UTF-8 BOM for Excel)
  download the underlying rows. Exports are **secret-free by design**: license
  passwords are never emitted, V2Ray `client_uuid` is masked and subscription
  URLs dropped, and no XUI credentials / tokens leave the panel. Five report
  **permissions** (`view_reports`, `view_financial_reports`, `view_user_reports`,
  `view_service_reports`, `export_reports`) gate pages, endpoints and exports per
  role, and every view / export writes an **audit** row (report name + date range
  + filters, never the data). The **dashboard** gained a 30-day analytics
  snapshot. **Not** in this phase: reseller, online payment gateway, Marzban /
  Hiddify adapters, backup / restore changes, accounting-grade invoices, external
  BI integrations.
- **Phase 12 — done.** **Backup, restore + operational maintenance.** Admins
  create **database / storage / full** backups from `/admin/maintenance/backups`
  (gzipped, under `storage/backups/YYYY/MM/`, mode `0600`, SHA-256 checksummed);
  database dumps use **`pg_dump`** in production (password via `PGPASSWORD`,
  never logged) with a SQLAlchemy fallback for dev/test. Backups are
  **verifiable** (recompute + compare checksum), **downloadable** (owner/admin
  only, `no-store`, path-traversal-safe — paths are resolved under
  `storage/backups` only), and **deletable**; a deleted backup can no longer be
  downloaded. **Restore is owner-only and confirmation-gated**: the app shows a
  verified plan, requires a signed time-limited token **and** typing
  `RESTORE_DIGITALCORE`, takes a **pre-restore backup first**, turns on
  maintenance mode, restores storage in-app, and — by design, for safety —
  delegates the destructive database restore to `scripts/restore.sh` on the
  server. **CLI**: `scripts/backup.sh {database|storage|full}`,
  `scripts/restore.sh`, `scripts/list_backups.sh`, and an extended
  `scripts/healthcheck.sh` (DB/Redis/disk/backup-size/recent-errors). A **health
  / diagnostics** page and a **system-info** page (no secrets) round it out, plus
  **maintenance-mode** controls and a worker **scheduled-backup + retention
  cleanup** sweep (off by default; never deletes the latest successful backup).
  Every action is audited (metadata only). **Not** in this phase: reseller,
  online payment gateway, Marzban / Hiddify adapters, advanced BI, multi-tenant.
- **Bot UX pass — done.** A user-experience fix pass for the Telegram bot (no new
  business domain): **product categories** so the bot shows *categories first →
  products in a category → a per-order pre-invoice* (پیش‌فاکتور) with wallet /
  card-to-card / gateway payment buttons; a **wallet top-up receipt fix** (any
  photo / PDF / image is accepted, the user always gets a detailed confirmation
  with the amount + request number, and no message type leaves the flow stuck); a
  working **«حساب من» (My Account)** page; the **rules** moved off the menu and
  shown on `/start`; a restructured main menu; and a **configurable license
  section title**. See **Bot UX — categories, invoice, account & receipts** below.
- **Sanaei 3X-UI integration rebuild — done.** The panel client was rebuilt from
  the Sanaei (MHSanaei/3x-ui) API: a clean async `SanaeiApiClient` with
  **Bearer API-token auth preferred** and cookie login as a fallback, safe
  (secret-free) logging, retries on transient failures, per-server TLS-verify and
  timeout, a rich **connection test** (auth + server status + inbounds), robust
  **inbound sync** that upserts and never deletes, and a **dry-run product
  validator**. See **Sanaei 3X-UI integration** below.

### Sanaei 3X-UI integration

The single client the app uses to reach a panel is
`app/services/sanaei_api_client.py` (`SanaeiApiClient`), with
`app/services/sanaei_adapter.py` building one from a stored `XuiServer`
(decrypting credentials, choosing the auth mode). Every panel endpoint path is a
named constant in one place.

**Authentication.** Two modes, chosen per server (`xui_servers.auth_mode`):

- **`api_token` (preferred).** A Bearer token is sent as
  `Authorization: Bearer <token>` on every request; no login round-trip. The
  token is a full-admin credential — stored **Fernet-encrypted**
  (`encrypted_api_token`) and never logged, rendered, or put in audit metadata.
- **`password` (fallback).** Username + password cookie login against
  `POST {base}{web_base_path}/login`, with one automatic re-login on a 401 /
  expired-session (a non-JSON HTML body is treated as an expired session).

`web_base_path` is honoured for panels served under a custom path. `tls_verify`
can be turned **off** only for self-signed panels; `timeout_seconds` is
per-server.

**Endpoints.** New `/panel/api/clients/*` (add/update/del) are tried first and
the client **falls back to the legacy `/panel/api/inbounds/*` flow on a 404**, so
both current and older Sanaei builds work. Reads use
`/panel/api/inbounds/list`, `/panel/api/inbounds/{id}`,
`/panel/api/inbounds/allLinks` and best-effort `/panel/api/server/status`
(for panel / xray version). Panel conventions are respected: **`expiryTime` in
milliseconds**, **`totalGB` in bytes** (despite the name), **email is the unique
client id**, and the panel-generated **`uuid` / `subId` are always adopted and
stored** rather than the locally-generated ones.

**Data model** (migration `0020_xui_auth_and_inbound_sync`, additive only —
existing rows keep working). `xui_servers` gains `auth_mode` (default
`password`), `tls_verify` (default on), `timeout_seconds` (default 20) and
`xray_version`. `xui_inbounds` gains `tag`, `enable_from_panel`, `raw_json` and
`synced_at`. Existing subscription fields (`public_sub_base_url`,
`subscription_path`) are reused.

**Admin.** The server form (`/admin/xui-servers/create` · `…/{id}/edit`) exposes
the auth method, API token, username/password (**blank keeps the stored secret**,
with a *configured / not set* badge), web base path, subscription base URL + path,
TLS-verify and timeout. **Test** and **Sync inbounds** run against the live panel
and record status / last error / panel + xray version (shown on the server list).
Inbound sync is an **idempotent upsert that never deletes** an inbound missing
from the panel.

**Validate a product before selling it.** From a V2Ray product's edit page,
**🔍 Validate binding** (`/admin/products/{id}/validate-binding`) runs a
**dry run** — it checks the product is bound to an active server + inbound, has a
duration + traffic quota, the server has credentials and a subscription host —
**without provisioning anything**. Add `?live=1` (the *Run live connection test*
button) to also probe auth + status + inbounds.

**Provisioning & lifecycle** stay idempotent: a deterministic client email
(`dc-u{user}-o{order}`), `find_client` before add so a retry after a partial run
repairs the local row instead of creating a second panel client, verify-after-
write on every change, and subscription links built only from a configured
`public_sub_base_url` (never guessed from the admin `base_url`).

**CLI.** `python scripts/test_xui_connection.py --server-id <id>` (or `--all`,
optionally `--sync`) prints a secret-free reachability report for a stored
server — handy for verifying a newly-added panel from the backend container.

### Bot UX — categories, invoice, account & receipts

**Product categories** (`app/models/product_category.py`, `product_categories`
table, migration 0019). A `ProductCategory` is a simple admin-managed grouping
(`title`, unique `slug`, `description`, `sort_order`, `is_active`); `Product`
gains a nullable `category_id` (FK, `ON DELETE SET NULL`) — existing products
stay uncategorised, so **no data backfill** is needed. Admin CRUD lives at
`GET/POST /admin/product-categories`, `…/create`, `…/{id}/edit`,
`…/{id}/toggle-active`; the product form gains a **category dropdown** (allow
none, validated against existing categories) and the product list shows the
category. `product_category_service.grouped_for_bot` groups **active** categories
that have **active, non-hidden, non-service-action** products (sorted by
`sort_order` then title), and collects every browsable product whose category is
NULL / inactive / deleted under a synthetic **«سایر محصولات» (Other)** group so
no purchasable product is ever orphaned.

**Bot product flow** (`app/bot/handlers/user/products.py`). *Products* now opens
a **category picker**; tapping a category lists its products (with a *back to
categories* button); tapping a product shows a **pre-invoice** — product, category,
description, price, and (for V2Ray) duration / traffic / IP / server — plus the
enabled **payment buttons**: *pay from wallet*, *card-to-card*, and *pay via
gateway*. When only the Other group exists (an operator that never sets up
categories), the bot skips straight to the flat product list — the old
experience. The **online gateway is a placeholder** gated by
`online_gateway_enabled` (default off): the button is shown only when enabled and
leads to a “coming soon” notice — **no real gateway is integrated**. Coupons and
the existing wallet / card charge paths are reused unchanged.

**Wallet top-up receipt fix** (`app/bot/handlers/user/wallet.py`). A submitted
receipt (photo, image, or **PDF/document**) is accepted, validated
(type/size → clear Persian errors), stored, and the top-up moves to
`waiting_admin`; the user immediately gets a **detailed confirmation** (“✅ رسید
شارژ کیف پول دریافت شد … مبلغ … شمارهٔ درخواست …”). The FSM is cleared so the flow
moves forward, an admin/log-group notification is sent best-effort, and a
**catch-all** handler guarantees any other content type (sticker, voice, …) still
gets guidance — the flow is **never silently stuck**.

**«حساب من» (My Account)** (`app/bot/handlers/user/account.py`). The previously
dead menu button now shows a summary — name, numeric Telegram id, username,
account status, wallet balance, and counts of orders / services / licenses — with
quick-link buttons to wallet, orders, services, the license section and support.
The **admin note is never exposed**; a restricted user sees a safe notice.

**Rules & main menu.** `قوانین` is **removed from the menu**; the rules text is
shown on `/start` (admins additionally see “تغییر قوانین از پنل مدیریت انجام
می‌شود”). The main menu is: محصولات · سفارش‌های من · سرویس‌های من ·
{license-section-title} · کیف پول · حساب من · آموزش‌ها · پشتیبانی (+ referral /
language). The **license section title** is configurable via the new
`license_section_title` setting (default «لایسنس‌های من»; e.g. set «اپل آیدی‌های
من») and is used for the menu button, the `/my_licenses` header, and its empty
state. **«سفارش‌های من»** now renders a readable multi-line block per order
(number, product, status, amount, payment method, date, rejection reason, delivery
status). New settings: `license_section_title` (texts) and
`online_gateway_enabled` (payment, default off).

**Bot UX follow-up (orders/licenses/menu).** «سفارش‌های من» and «لایسنس‌های من»
are now **paginated inline lists** (5/page) with per-item **detail views** —
orders show number/product/category/status/method/amounts/dates/reject-reason;
licenses show a **numbered list** (order number + product), and tapping one opens
its credentials **only for the owner of that order**; navigation edits the message
in place (`edit_message_text`) and falls back to a fresh message. **Language is
removed from the menu**: the bot language follows the `bot_default_language`
setting (fa/en, default fa) and is no longer asked on `/start`; users can still
switch with `/language`, and a cached «زبان» button explains it's admin-managed.
The «منوی کاربر» button now reliably returns the user menu (it was crashing) and
clears any pending FSM flow. Admin web: `/admin/dashboard` redirects to `/admin`,
the dashboard degrades gracefully if report data fails, and a safe
**`/admin/notifications`** («اطلاع‌رسانی») page + sidebar link are added.

**Deploying this change (runtime).** The category feature adds migration **0019**
and maps `Product.category_id`. Because the container entrypoint does **not**
auto-migrate by default, a deploy that only rebuilds images without running
migrations leaves the DB one revision behind — then every product/account query
raises `column products.category_id does not exist` and the bot's products,
account and order flows fail silently. After pulling, always run:

```bash
docker compose build backend bot worker
docker compose up -d backend bot worker
docker compose exec -T backend alembic upgrade head   # ← required
docker compose restart bot
```

(`scripts/update.sh` already does this.) Set `AUTO_MIGRATE=true` in the
environment to have the backend apply migrations on start. To root-cause a "bot
not OK" report in one command, run the read-only diagnostic — it flags a stale
schema, counts categories/products, and prints the bot-relevant settings:

```bash
docker compose exec -T backend python scripts/debug_bot_state.py
```

Admins get the same report in-bot via **`/debug_bot_state`**. Reply-keyboard
buttons are matched **tolerantly** (`app/i18n/menu_texts`): both the emoji label
and a typed/de-emojified variant route correctly, so a user with an older cached
keyboard is not stuck. If no categories exist yet, the bot shows the flat product
list — create categories at `/admin/product-categories` to get the category picker.

### Phase 12 — backup, restore & maintenance

**Backup** (`app/services/backup_service.py`, `backup_jobs` table, migration
0018). A `BackupJob` records **metadata only** — type, status, on-disk path
(under `storage/backups/`), size and SHA-256 — never the backup contents.
`create_backup_job` / `run_backup_job` orchestrate; `create_database_backup`
(pg_dump → gzip, SQLAlchemy fallback for dev/test), `create_storage_backup`
(tar.gz of receipts/tickets/exports/qrcodes/uploads, **excluding**
`storage/backups`), and `create_full_backup` (db + storage + `metadata.json` +
`RESTORE.txt`) produce the files at mode `0600`. `verify_backup` recomputes the
checksum; `cleanup_old_backups` prunes by age but **always keeps the newest
`backup_keep_last`** and never the single latest. Every `error_message` is
scrubbed of the DB password and URL credentials before it is stored or logged.

**Restore** (`app/services/restore_service.py`) is deliberately safety-first.
`validate_backup_file` / `inspect_backup` are read-only (path must resolve under
`storage/backups`; gzip magic; archive members listed, never extracted blindly).
`create_restore_plan` returns a plan + warnings + the exact CLI command with no
side effects. A signed, 10-minute **confirmation token** bound to (admin,
backup) plus the typed `RESTORE_DIGITALCORE` phrase are both required. The
guarded entry points verify the token + backup checksum, take a **pre-restore
backup first**, enable maintenance mode, restore storage in-app (with a
tar-traversal guard), and delegate the destructive DB overwrite to the CLI.

| Area | Routes / scripts |
|------|------------------|
| Web | `GET /admin/maintenance` + `/backups` (`POST /create`, `GET /{id}`, `GET /{id}/download`, `POST /{id}/{verify,delete}`), `GET /restore` (`POST /restore/{plan,confirm}`), `POST /maintenance/mode`, `GET /health`, `GET /system-info` |
| CLI | `scripts/backup.sh {database\|storage\|full}`, `scripts/restore.sh <file> [--yes]`, `scripts/list_backups.sh`, `scripts/healthcheck.sh` |
| Worker | hourly sweep: retention cleanup + optional scheduled backup (off by default) |

**Settings** (defaults): `backups_enabled` (true), `backup_download_enabled`
(true), `scheduled_backups_enabled` (**false**), `scheduled_backup_type` (full),
`scheduled_backup_hour` (3, UTC), `backup_retention_days` (7), `backup_keep_last`
(5), `maintenance_message`. **Permissions**: `view_maintenance` + `view_health`
(owner/admin/support), `manage_backups` + `download_backups` (owner/admin),
`restore_backups` (**owner only**). **Security**: backups are never served
publicly (only the auth-gated, `no-store`, traversal-checked download route);
restore is owner-only, token- and phrase-gated, and makes a rollback backup
first; audit logs and `error_message`s never contain secrets, the DB URL
password, or backup contents; `system-info` shows the DB *backend name* only,
never the URL. Off-site copies should still be encrypted at the storage layer.

### Phase 11 — reports, analytics & CSV exports

**Report service** (`app/services/report_service.py`) is the read-only
aggregation layer. `parse_date_range` / `get_previous_period` /
`safe_percent_change` handle the date maths (presets + custom, previous-period
deltas, zero-baseline-safe percentages). Dashboard summaries
(`get_dashboard_summary`, `get_revenue_summary`, `get_order_summary`,
`get_user_summary`, `get_product_summary`) plus per-area functions for sales,
orders, payments, wallet, products, users, licenses and V2Ray return plain
dict / list structures safe for both templates and JSON. Money is **integer
toman** (the platform convention — no Decimal money anywhere). Day buckets use
`func.date(col)`, verified on **both SQLite (tests) and PostgreSQL (runtime)**.
Optional Phase 9/10 models (tickets, coupons, referrals) are imported
defensively — `*_AVAILABLE` flags degrade the matching report to
`{"available": False}` instead of crashing if a checkout predates those phases.

**Export service** (`app/services/export_service.py`) renders UTF-8-with-BOM CSV
via the stdlib `csv` module, one bounded query per export (`MAX_ROWS` cap,
logged never silent). `export_orders_csv`, `export_payments_csv`,
`export_wallet_transactions_csv`, `export_users_csv`, `export_products_csv`,
`export_licenses_csv`, `export_v2ray_services_csv`, and the optional
`export_coupon_usages_csv` / `export_referral_rewards_csv` / `export_tickets_csv`
each emit only non-secret columns.

| Area | Routes |
|------|--------|
| Report pages | `GET /admin/reports` + `/sales /orders /payments /wallet /products /users /licenses /v2ray /marketing /support /exports` |
| Chart JSON | `GET /admin/reports/api/{sales-by-day,user-growth,orders-by-status,payments-by-method,top-products,v2ray-usage}` |
| CSV exports | `GET /admin/reports/export/{orders,payments,wallet-transactions,users,products,licenses,v2ray-services,coupon-usages,referral-rewards,tickets}.csv` |

**Permissions / roles** — `view_reports` (overview / orders / products / support):
owner, admin, accountant, support, viewer. `view_financial_reports` (sales /
payments / wallet / marketing): owner, admin, accountant. `view_user_reports`:
owner, admin, support. `view_service_reports` (licenses / v2ray): owner, admin,
support. `export_reports`: owner, admin, accountant. So **viewer** sees the
overview but cannot export or open financial / user / service drill-downs;
**accountant** gets the money reports + exports but not user reports; **support**
gets user + service reports but no money and no export. **Security**: exports
never include license passwords (no route, no column), V2Ray UUIDs are masked
and subscription URLs / XUI credentials are never exported, and users export
omits phone numbers and internal notes. Every view / export is audited
(`report.viewed`, `report.financial_viewed`, `report.user_viewed`,
`report.service_viewed`, `report.export_created`) with the report name, date
range and filters — never the exported data.

### Phase 10 — coupons & referrals

**Coupons** (`app/services/coupon_service.py`, `coupons` + `coupon_usages`,
migration 0017). A coupon carries a percent (1–100, optionally capped by
`max_discount_amount`) or fixed discount, `min_order_amount`, `usage_limit` +
`usage_limit_per_user`, a `starts_at`/`expires_at` window, and optional
restrictions (`product_id`, `product_type` ∈ license/v2ray, `applies_to_action`
∈ new_purchase/renew_service/add_traffic). `validate_coupon` returns a precise
error code (`coupon_not_found`, `coupon_expired`, `usage_limit_reached`,
`min_order_amount_not_met`, `product_type_not_allowed`, …); `apply_coupon_to_order`
sets `final_amount = amount − discount` on a still-pending order (frozen once a
receipt is submitted / payment approved), and `record_usage` consumes it on
payment — race-safe + idempotent. Wallet payment charges `final_amount`; the
card-to-card instructions and the admin order page both show the coupon + discount.

**Referrals** (`app/services/referral_service.py`, `referral_rewards`; `users`
gains `referral_code` + `referral_registered_at`, reusing the existing
`referrer_id`). `register_referral` is safe/idempotent (ignores invalid / self,
never overwrites). `create_reward_for_order` runs after a paid order is
**delivered** (hooked in both the wallet-pay and card-approve paths), honours
`referral_reward_first_order_only` + `referral_min_order_amount`, computes a fixed
or percent reward, and either auto-pays it to the referrer's wallet (a `reward`
wallet transaction) or leaves it `pending`. Admin approve/pay/reject are a
row-locked, status-guarded wallet payout that never double-credits.

| Area | Routes / commands |
|------|-------------------|
| Bot user | Buy → coupon prompt (enter / skip) → discounted method picker; `/coupons` (public list); `/referral`, دعوت دوستان (link + code + stats); `/start ref_<code>` |
| Web coupons | `GET /admin/coupons` (+ `/create`, `/{id}/edit`, `/{id}/usages`), `POST /admin/coupons/{create,{id}/edit,{id}/deactivate}` |
| Web referrals | `GET /admin/referrals`, `GET /admin/referral-rewards`, `POST /admin/referral-rewards/{id}/{approve,pay,reject}` |

**Settings** (Marketing): `coupons_enabled`, `show_public_coupons`,
`referrals_enabled`, `referral_reward_enabled`, `referral_reward_type`,
`referral_reward_value`, `referral_reward_requires_admin_approval`,
`referral_reward_first_order_only`, `referral_min_order_amount`.
**Permissions**: `view_coupons` (list/usages — owner/admin/support/accountant/viewer),
`manage_coupons` (CRUD — owner/admin), `manage_referrals` (reward approve/pay/reject —
owner/admin/accountant). All coupon validation is server-side; users cannot touch
another user's order coupon flow or self-refer. Audited: `coupon_created/_updated/
_deactivated/_applied/_removed/_consumed`, `referral_registered`,
`referral_reward_created/_approved/_rejected/_paid`.

### Phase 9 — support tickets & tutorials

**Tickets** (`app/services/ticket_service.py`, `tickets` + `ticket_messages`
tables). A ticket is a thread whose status tracks who owes the next move
(`open → pending_admin ⇄ pending_user → closed`). A user reply flips it to
`pending_admin`, a staff reply to `pending_user`; either side can close it and —
when `allow_reopen_closed_tickets` is on — the user can reopen. **Ownership is
strict**: `add_user_reply` / `reopen_ticket` / user-close verify `user_id`, and
the bot/web never surface another user's ticket. Attachments reuse the
payment-receipt discipline (sanitised filename, type + size validation against
`max_ticket_attachment_mb`, on-disk under the tickets root, traversal-guarded
serving). Every action is audited (`ticket_created`, `ticket_user_replied`,
`ticket_admin_replied`, `ticket_closed`, `ticket_reopened`, `ticket_assigned`,
`ticket_priority_changed`).

**Tutorials** (`app/services/tutorial_service.py`, `tutorial_categories` +
`tutorials` tables). Categories group articles; each tutorial has a generated,
de-duplicated slug, optional `platform` (android/ios/windows/mac/linux/general)
and `product_type` (license/v2ray/general), and an `is_active` flag that hides
drafts from users while admins see everything. Content is stored verbatim and
rendered HTML-escaped with `<br>` line breaks — there is no Markdown/HTML
sanitiser dependency, so untrusted HTML is never injected. Audited:
`tutorial_created`, `tutorial_updated`, `tutorial_toggled`,
`tutorial_category_created`, `tutorial_category_updated`.

| Area | Routes / commands |
|------|-------------------|
| Bot user | `/support`, `/tickets`, پشتیبانی, تیکت‌های من (create / list / open / reply / close / reopen); `/tutorials`, آموزش‌ها |
| Bot admin | `/admin_tickets`, `/admin_ticket <number>` → reply · close · assign · priority |
| Web tickets | `GET /admin/tickets` (+ `?status=`, `?assigned=me`), `GET /admin/tickets/{id}`, `POST …/{reply,close,assign,priority}`, `GET /admin/tickets/attachments/{message_id}` |
| Web tutorials | `GET/POST /admin/tutorials`, `…/create`, `…/{id}/edit`, `…/{id}/toggle-active`, `GET/POST /admin/tutorial-categories` (+ `…/{id}/edit`) |

**Settings**: `support_enabled`, `ticket_attachments_enabled`,
`max_ticket_attachment_mb` (10), `allow_reopen_closed_tickets`, `tutorials_enabled`.
**Permissions**: `view_tickets` (list/detail — owner/admin/support/accountant/viewer),
`manage_tickets` (reply/close/assign/priority — owner/admin/support),
`manage_tutorials` (author tutorials — owner/admin). Viewer is read-only;
accountant can view tickets but not manage them.

### Phase 8 — renewal, add-traffic & service lifecycle

**Service actions** (`app/services/v2ray_lifecycle_service.py`). A renew /
add-traffic **product** is a v2ray product with `applies_to_service = true` and an
`action_type` (`renew_service` / `add_traffic`); such products are hidden from the
normal catalog and only reachable from a specific service. An admin creates one on
the **product form** (`/admin/products/create`) by ticking *“Service-action
product”* and choosing renew or add-traffic — the form drops the server/inbound
binding (a service action reuses the target service's binding) and validates that a
renewal has a duration and an add-traffic product has traffic. Buying one creates an
order carrying `action_type` + `target_service_id` (validated: the target must be
the buyer's own, non-deleted service, and the product must match the action).
`renew_service` extends the expiry and re-enables the panel client
(verify-after-write); `add_traffic` grows `total_gb` and clears an over-quota
state. Both compute the new fields, write+verify on the panel, and **only then**
mutate the local row — so a panel failure leaves the service untouched.

**Order-driven delivery** — `apply_service_action_for_order` locks the order row
`FOR UPDATE` for the whole operation (no intermediate commit), guards on
`status == "delivered"` for idempotency, and on a panel error marks the order
`provisioning_pending` + a safe `delivery_error` **without** undoing the payment.
The delivery dispatcher routes a v2ray order to provisioning (Phase 6) or to this
lifecycle path by `action_type`. Admins can retry a failed action from the order
page.

**Worker lifecycle sweep** (`app/worker/main.py` → `lifecycle_tick`, error-isolated
per step): DB-only marking of `expired` / `over_quota`; an interval-gated, batched
usage refresh from the panel (`v2ray_usage_refresh_*`); optional panel
auto-disable of expired / over-quota clients (`v2ray_auto_disable_*`, idempotent
via `disabled_at`); and one-shot expiry / traffic warnings (guarded by
`last_*_warning_at`, reset on renew / add-traffic so a later cycle can warn again).

| Area | Routes / commands |
|------|-------------------|
| Bot user | `/my_services` detail (live usage / remaining / days) → **Get link · Refresh · Renew · Add traffic** → plan → method picker → pay |
| Web | `POST /admin/v2ray-services/{id}/{renew,add-traffic}`, `POST /admin/orders/{id}/retry-v2ray-provisioning` (renew/add-traffic aware) |

**Settings**: `v2ray_usage_refresh_enabled` (true), `v2ray_usage_refresh_interval_minutes`
(60), `v2ray_expiry_warning_days` (3), `v2ray_traffic_warning_percent` (90),
`v2ray_auto_disable_expired` (true), `v2ray_auto_disable_over_quota` (true).
**Security / permissions**: users can only renew / add-traffic / view their **own**
service; admin lifecycle controls need `manage_services`; no XUI credential is ever
logged. Audited: `v2ray_action_started/_delivered/_failed`, `v2ray_service_renewed`,
`v2ray_traffic_added`, `v2ray_service_auto_disabled`, plus the Phase 6 management
actions.

### Phase 7 — wallet top-up & wallet payment

**Top-up** (`app/services/wallet_service.py`, `wallet_topup_requests` table):
`/wallet` → top-up asks an amount (validated against `min_wallet_topup` /
`max_wallet_topup` / `wallet_topup_enabled`), opens a request, shows the
card-to-card instructions, and takes a receipt (same validator + on-disk store
as order receipts, under `storage/receipts/wallet/YYYY/MM/`). The request moves
`pending_receipt → waiting_admin`; admins approve (credits the wallet, type
`deposit`) or reject (with a reason) from the web or the Telegram buttons. A
second approve/reject fails safely.

**Wallet payment** — the Buy button now offers a **payment-method picker**
(card-to-card / wallet) when both are enabled. Paying by wallet locks the user
row, checks the balance, records a `purchase` transaction, marks the payment
approved + order approved, and runs the existing delivery dispatcher — all under
one lock with no intermediate commit, so a double-tap charges **once**. An
insufficient balance shows the shortfall and a top-up button; the card-to-card
receipt flow is unchanged.

**Refund** — `Refund to wallet` on an approved/delivered/failed order credits the
buyer (type `refund`), stamps `orders.refunded_at` + `payments.refunded_amount`,
and is idempotent (a second refund is refused). Full refunds only.

| Area | Routes / commands |
|------|-------------------|
| Bot user | `/wallet` (balance + top-up + history), method picker on Buy |
| Bot admin | top-up notification with ✅/❌ buttons (`manage_wallet_topups`) |
| Web top-ups | `GET /admin/wallet/topups` (+ `/pending`, `/{id}`), `POST …/{id}/{approve,reject}` |
| Web | `GET /admin/wallet/transactions`, `GET /admin/wallet/receipts/{id}`, `POST /admin/orders/{id}/refund` |

**Settings**: `wallet_enabled`, `wallet_topup_enabled`, `wallet_payment_enabled`,
`min_wallet_topup`, `max_wallet_topup` (0 = unlimited), `allow_negative_wallet`.
**Security / permissions**: receipts are served only to admins with
`view_wallet_topups` (traversal-guarded); `manage_wallet_topups` approves/rejects
(owner/admin/accountant); `refund_payments` refunds; support views only; balances
never go negative unless `allow_negative_wallet`. Audited: `wallet_topup_created`,
`wallet_topup_receipt_submitted`, `wallet_topup_approved/_rejected`,
`wallet_payment_started/_completed`, `wallet_insufficient_balance`,
`wallet_refund_created`, `wallet_balance_added/_subtracted`.

### Phase 6 — real 3X-UI V2Ray provisioning

**Provision** (`app/services/v2ray_service.py`, `v2ray_services` table): approving
a V2Ray order calls the dispatcher, which builds a `ClientAdd`
(`expiryTime` = now + `duration_days` in **ms**, `totalGB` = `traffic_gb` in
**bytes**, `limitIp` = `ip_limit` or 1), calls `xui_service.add_client`
(login → addClient → **verify-after-write**), then stores a local
`V2RayService` (`active`) and marks the order `delivered`. The client email is
**deterministic** (`dc-u{user}-o{order}`); before adding, we `find_client` on the
panel so a retry after a partial run **reuses** the existing client instead of
duplicating it. The order row is locked `FOR UPDATE` for the whole operation
(no intermediate commit) so two concurrent provisions serialize — the loser sees
the active service and returns it. `order_id` is unique in `v2ray_services` as a
DB backstop.

**Subscription + QR** — a subscription URL is built **only** when an admin has set
the server's `public_sub_base_url` (+ optional `subscription_path`, default
`/sub/`); otherwise it is left null and the user is told support will follow up
(we never leak the private admin base URL). When a URL exists a QR PNG is written
under `storage/exports/qrcodes/<id>.png` (`qrcode[pil]`, imported lazily — a
missing lib degrades to text-only, never a crash).

**Failure / retry** — a failure (server/inbound missing or inactive, auth error,
verify mismatch) never rolls back the approval: the order stays
`approved`/`provisioning_pending` with a safe `delivery_error`, the service row is
`failed`, and an admin retries from the service or `POST
/admin/orders/{id}/retry-v2ray-provisioning`. A Telegram send failure does **not**
fail provisioning (the client already exists; the user re-fetches via
`/my_services`).

| Area | Routes / commands |
|------|-------------------|
| Web list/detail | `GET /admin/v2ray-services` (status filter), `GET /admin/v2ray-services/{id}` |
| Web actions | `POST …/{id}/{refresh-usage,disable,enable,delete,reset-traffic}`, `POST /admin/orders/{id}/retry-v2ray-provisioning` |
| Bot user | `/my_services` (My Services) — own services only, re-send link + QR |
| Bot admin | `/admin_v2ray`, `/admin_v2ray_failed` — counts + failed list |

**Security / permissions**: no XUI password/token/cookie is ever stored, logged,
or rendered; the client UUID is masked on the detail page. `view_services`
(owner/admin/support/accountant/viewer) sees pages + refresh; `manage_services`
(owner/admin) runs disable/enable/delete/reset/retry. Audited:
`v2ray_provision_started/_retry`, `v2ray_client_created/_verified`,
`v2ray_provision_failed`, `v2ray_service_delivered`,
`v2ray_service_user_notified/_notification_failed`,
`v2ray_service_usage_refreshed`, `v2ray_service_disabled/_enabled/_deleted`,
`v2ray_traffic_reset`. A DB-only worker sweep marks expired services `expired`
(no panel calls, so it can never spam a server).

### Phase 5 — license stock & real license delivery

**Stock + import** (`app/services/license_service.py`, `license_items` table):
licenses are `EMAIL:/PASSWORD:/NOTE:` blocks (blank-line separated, keys
case-insensitive, note optional) or `email,password,note` CSV lines. Import
reports imported / duplicate-in-file / duplicate-in-DB / invalid counts and never
imports malformed blocks silently. A license moves `available → reserved → sold`;
admins can also `blocked`/`broken`/`replaced` it.

**Delivery** — approving a license order calls the dispatcher, which reserves one
`available` license with `SELECT … FOR UPDATE SKIP LOCKED` (a real lock on
Postgres), sends the credentials to the buyer, then flips it to `sold` and the
order to `delivered`. It is **idempotent**: re-approving or re-delivering a
delivered order never sells a second license. If stock is empty or the Telegram
send fails, the license stays reserved/attached and the order keeps a
`delivery_error` so an admin can **redeliver**. **Replacement** swaps the sold
license for a fresh one (old → `replaced`, `replaced_by_license_id` set) and
re-notifies the user.

| Area | Routes / commands |
|------|-------------------|
| Web stock | `GET /admin/licenses` (product+status filters), `/admin/licenses/sold`, `/admin/licenses/low-stock` |
| Web import | `GET/POST /admin/licenses/import` (license products only) |
| Web detail + actions | `GET /admin/licenses/{id}`, `POST …/{id}/{block,mark-broken,redeliver,replace}` |
| Bot user | `/my_licenses` (My Licenses) — own licenses only |
| Bot admin | `/admin_licenses`, `/admin_license_stock` — available/sold + low-stock |

**Low stock** — the `license_low_stock_threshold` setting (default **5**) drives
the Low stock page and a `low_stock_detected` audit when a sale drops stock below
it. **Security**: passwords are stored (delivery needs them) but never appear on
list pages, never in audit metadata (emails are masked, e.g. `a***@x.com`), and a
password shows on the detail page only with `view_license_secrets`. Permissions:
owner/admin manage + import + view secrets; support view-only; accountant/viewer
counts only. Audited: `license_added`, `license_bulk_imported`,
`license_import_failed`, `license_reserved`, `license_sold`, `license_delivered`,
`license_delivery_failed`, `license_redelivered`, `license_marked_broken`,
`license_blocked`, `license_replaced`, `low_stock_detected`.

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
- *v2ray* → **in Phase 4** this only parked the order at `provisioning_pending`
  (no client was created). Real 3X-UI provisioning arrives in **Phase 6**
  (`v2ray_service.provision_service_for_order`).

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
`orders.delivered_payload`, and a first `license_keys` pool table); Phase 5 adds
migration **0012** (the real `license_items` stock table + `orders.delivery_error`,
and drops the superseded `license_keys` table). All run cleanly on a fresh **and**
an existing database and preserve operator-entered values.

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

1. Import licenses for a license product (see the Phase 5 import page).
2. As a user, order the product and submit a receipt (Phase 3 flow).
3. In **Orders → Pending receipts** (or the Telegram notification), open the receipt.
4. Click **Add balance**, enter an amount + reason → the user's wallet increases,
   a wallet transaction appears, and an audit row is written.
5. Click **Block user** → the user can no longer use the bot; **unblock** from the
   user detail page.
6. Click **Restrict user** with a reason → the user can `/start` but cannot create
   a new order or submit a receipt; **unrestrict** to reverse it.
7. Click **Approve** → a license order becomes `delivered` (see Phase 5); a V2Ray
   order parks at `provisioning_pending`.
8. Reject a different receipt with a reason; confirm duplicate approve/reject is
   blocked.
9. Repeat the same actions from the **Telegram** notification buttons (approve,
   reject, add/subtract balance, block, restrict, view user) — confirm only the
   admin who started an FSM action can finish it, and `/cancel` aborts.

## Phase 5 manual test checklist

1. Log in; create or use a **license** product.
2. Open **Licenses → Import licenses**, choose the product, and paste two blocks:
   `EMAIL: test1@example.com / PASSWORD: pass1 / NOTE: note1` and a second one.
3. Confirm the import result (imported 2) and that **License stock** shows them as
   `available` — with **no password** in the list.
4. In the bot, buy the product, submit a receipt, and **approve** it.
5. Confirm the buyer receives the EMAIL / PASSWORD / NOTE in Telegram, the order is
   `delivered`, the payment `approved`, and the license `sold` with
   `sold_to_user_id` + `order_id` set.
6. `/my_licenses` shows the license; re-approving the same order sells **no** second
   license; **redeliver** re-sends the same one.
7. Confirm **Licenses → Low stock** warns when available drops below the threshold,
   and that no license password appears in the audit log.

## Phase 6 manual test checklist

1. Log in; under **V2Ray / 3X-UI** add (or reuse) a server, **test connection**,
   and **sync** (or add) an inbound.
2. Optionally set the server's **public subscription host** so subscription links
   can be built (otherwise the service is delivered without a link).
3. Create an active **V2Ray** product bound to that server + inbound (with
   duration, traffic, IP-limit).
4. In the bot, buy the product, submit a receipt, and **approve** it.
5. Confirm a client is created on the correct server/inbound, **verify-after-write**
   succeeds, a `V2RayService` row exists, and the order becomes `delivered`.
6. Confirm the buyer receives the subscription link (+ QR if configured) in
   Telegram, and `/my_services` shows the service and can re-send the link.
7. Confirm **Admin → V2Ray Services** lists it; open the detail page and confirm
   **no XUI credentials** appear and the UUID is masked.
8. Re-approve / retry the same order and confirm **no duplicate** client is created.
9. Disable / enable / reset-traffic / refresh-usage from the detail page (with
   `manage_services`); confirm a `viewer` is blocked from those actions.
10. Confirm audit rows exist for provisioning and do not leak the panel password.

## Phase 7 manual test checklist

1. Log in; set the card-to-card settings; ensure **Wallet enabled** and **Wallet
   top-up enabled**.
2. In the bot send `/wallet`, request a top-up amount, and send a receipt.
3. Confirm it appears under **Admin → Wallet → Pending top-ups**; approve it.
4. Confirm the user's wallet balance increased and a `deposit` transaction exists.
5. Buy a product and choose **Wallet**; confirm the balance decreases and the
   license is delivered (or the V2Ray service is provisioned).
6. Try buying with insufficient balance; confirm the shortfall message and that
   no balance changed.
7. Re-tap wallet pay on the same order; confirm it is **not** charged twice.
8. Refund the order to the wallet; confirm the balance increases and a second
   refund is refused.
9. Confirm `/wallet` history shows deposit / purchase / refund, and that audit
   rows exist without leaking secrets.

## Phase 8 manual test checklist

1. On the product form create a normal v2ray product, then a **renewal** product
   (tick *Service-action product* → **Renew** + a duration) and an **add-traffic**
   product (tick *Service-action product* → **Add traffic** + a traffic amount);
   note the binding fields disappear for those. Confirm the two action products
   show their pill in the list and do **not** appear in the bot's buy catalog.
2. Buy the base product and let it provision; open `/my_services` → the service.
   Confirm it shows live status, remaining traffic, and days left.
3. Tap **Renew**, pick the plan, pay (card or wallet); confirm the expiry moves
   out by the plan's duration and the service is active.
4. Tap **Add traffic**, pick the package, pay; confirm the quota grows.
5. Force a failure (stop the panel) and retry the renew from the order page;
   confirm the payment was **not** undone and the order is retryable.
6. Set a service's expiry into the past / usage over quota and run the worker;
   confirm it is marked `expired` / `over_quota` and (if enabled) auto-disabled.
7. Confirm expiry / traffic warnings fire **once** as a service approaches its
   limit, and again after a renew / add-traffic.
8. Confirm audit rows exist for the actions and no XUI credential is logged.

## Phase 9 manual test checklist

1. In the bot send `/support`, create a ticket (subject → message).
2. Reply again to the ticket, this time attaching an image or document.
3. Confirm the ticket appears under **Admin → Support → Tickets** (`/admin/tickets`).
4. Open it, reply from the web (optionally attach a file), and confirm the
   attachment opens via the auth-gated link.
5. Confirm the user receives a Telegram notification of the reply.
6. From Telegram as the owner, run `/admin_tickets`, open one, reply, set a
   priority, and assign it to yourself.
7. As the user, reply again, then close the ticket; reopen it if the setting allows.
8. Confirm another user cannot open your ticket (bot shows "not found").
9. Create a tutorial **category**, then an **Android V2Ray** tutorial.
10. In the bot open `/tutorials`, browse to the tutorial, and read it.
11. Buy/deliver a V2Ray service and confirm the **connection-guide** button appears
    under the delivery message and opens the tutorial.
12. Toggle the tutorial inactive and confirm it disappears from the bot.
13. Confirm audit rows exist for ticket + tutorial actions and no secrets leak.

## Phase 10 manual test checklist

1. Admin creates a **percent** coupon at `/admin/coupons/create`.
2. In the bot, buy a product → tap **enter a discount code** → apply it.
3. Confirm the final amount is discounted (bot shows original / discount / final).
4. Pay by **wallet** and confirm the wallet is charged the discounted amount.
5. Repeat with **card-to-card** and confirm the instructions show the discounted total.
6. Confirm the coupon's usage count increases after the order is paid.
7. Create an expired or product-restricted coupon and confirm it is rejected.
8. Enable referral rewards; open `/referral` and copy the invite link.
9. Start the bot from a second account via the referral link.
10. Complete the referred account's first purchase through to delivery.
11. Confirm a referral reward is created; with auto-payout, the referrer's wallet
    increases; with approval required, approve it at `/admin/referral-rewards`.
12. Confirm no duplicate reward is created for the same order.
13. Confirm audit rows exist for coupon + referral actions and no secrets leak.

## Phase 11 manual test checklist

1. Log in as **owner/admin** and open `/admin/reports`.
2. Check the overview cards (revenue, delivered orders, new / active users) and
   the "needs attention" panel.
3. Open the **sales** report; switch the date range (today / 7-day / 30-day /
   this-month / custom start–end) and confirm the figures update.
4. Open the **orders**, **payments**, **wallet**, **products**, **users**,
   **licenses**, **V2Ray**, **marketing** and **support** reports in turn.
5. Export **orders**, **payments** and **users** CSV from the export buttons.
6. Open each CSV in Excel/Sheets and confirm it has a header row and readable
   Persian/Unicode text (UTF-8 BOM).
7. Confirm the exports contain **no secrets** — no license passwords, no full
   V2Ray UUID (masked `****xxxx`), no subscription URLs, no XUI credentials, no
   user phone numbers.
8. Log in as **accountant** — confirm sales / payments / wallet / marketing and
   the exports work, but `/admin/reports/users` is **denied (403)**.
9. Log in as **support** — confirm users / licenses / V2Ray reports work, but
   `/admin/reports/sales` and any export are **denied (403)**.
10. Log in as **viewer** — confirm the overview loads but every export and the
    financial / user / service drill-downs are **denied (403)**.
11. Confirm `AuditLog` rows exist for `report.viewed` /
    `report.financial_viewed` / `report.export_created` (with the report name +
    date range), and that the exported data itself is **not** in the audit log.

## Phase 12 manual test checklist

1. Log in as **owner** and open `/admin/maintenance`.
2. Open the **backups** page.
3. Create a **database** backup, a **storage** backup, and a **full** backup.
4. **Verify** a backup — confirm the checksum result is OK.
5. **Download** a backup; confirm the browser saves the `.gz` file and that
   downloading requires auth (`no-store`, owner/admin only).
6. Confirm **path traversal is blocked** — a tampered path never serves an
   arbitrary file (the route returns 404, never `/etc/passwd`).
7. Open the **restore** page; confirm the strong warning, and that restore
   requires the owner role, a valid token, and typing `RESTORE_DIGITALCORE`.
8. On the server, run `bash scripts/backup.sh full`, then
   `bash scripts/list_backups.sh` and `bash scripts/healthcheck.sh`.
9. **Enable** maintenance mode from the maintenance page; confirm normal bot/web
   users see the maintenance notice while admins keep access; then **disable** it.
10. Confirm `AuditLog` rows exist for `backup_job_created` / `backup_completed` /
    `backup_downloaded` / `backup_verified` / `backup_deleted` /
    `restore_plan_created` / `maintenance_mode_enabled` / `health_check_viewed`,
    and that no secrets (DB URL/password, backup contents) appear in them.
11. Confirm old-backup cleanup keeps the newest `backup_keep_last` and **never
    deletes the latest** successful backup; a **deleted** backup can no longer be
    downloaded (404).

## CI / GitHub Actions

Continuous integration runs on **every pull request to `main`**, **every push to
`main`**, and on demand (`workflow_dispatch`) via
[`.github/workflows/ci.yml`](.github/workflows/ci.yml) — the workflow is named
**DigitalCore CI**. It uses only dummy CI values: no real Telegram token, no real
3X-UI panel, and no production `.env`. XUI and network-dependent tests are mocked,
so CI passes on a fresh clone.

Jobs:

| Job | What it checks |
|-----|----------------|
| **lint-test** | `python -m compileall app migrations tests`, `python -m pytest -q` (SQLite fallback), `bash -n` on `install.sh` + `scripts/*.sh`, and `docker compose config` |
| **migration-check** | Runs `alembic upgrade head` + `alembic current` against a fresh **postgres:15** service (with **redis:7** available) |
| **docker-build** | `docker compose config` and `docker compose build backend bot worker` using a dummy `.env` |
| **optional-smoke** | Best-effort, **non-blocking** (`continue-on-error`): boots postgres + redis + backend, applies migrations, and curls `/health` + `/ready`. Runs on push-to-main and manual dispatch only (skipped on PRs). |

**Do not merge a PR while CI is failing.** The PR template
(`.github/pull_request_template.md`) carries a pre-merge checklist, and
`.github/dependabot.yml` opens weekly dependency PRs for `pip` and
`github-actions`.

Reproduce the core checks locally:

```bash
python -m compileall app migrations tests
python -m pytest -q
bash -n install.sh
find scripts -name "*.sh" -type f -print0 | xargs -0 -r bash -n
docker compose config
```

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

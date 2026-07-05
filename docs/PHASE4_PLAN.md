# Phase 4 Plan — Approval / Delivery + Admin Quick Actions from Receipt Review

> Status: **planned, not yet implemented.** This document captures the agreed
> scope so Phase 4 is built correctly in one pass. It folds in the "Admin quick
> actions from receipt review" requirement.

## 1. Scope

**Core Phase 4** (approval → delivery):
- Approve / reject a submitted receipt; drive the order/payment state machine
  (`waiting_admin → approved/rejected`, payment `receipt_submitted →
  approved/rejected`), set `paid_at`/`approved_at`/`rejected_at`, `admin_id`,
  `reject_reason`.
- Delivery after approval: issue license keys for `license` products; create the
  3X-UI client + subscription/QR for `v2ray` products (using the Phase 2.1
  server/inbound binding); set `delivered`/`delivered_at` and notify the user.

**Addendum — quick admin actions while reviewing a receipt** (this document's
focus), available from both the web panel and the Telegram admin panel:
1. Approve receipt · 2. Reject receipt (with reason) · 3. Add wallet balance ·
4. Subtract wallet balance · 5. Block user · 6. Restrict user · 7. View user ·
8. Open order in panel.

**Explicit non-goals (unchanged):** no wallet *purchase*/top-up flow, no payment
gateway, no coupons/referrals/tickets/reports/reseller. Wallet actions here are
**manual admin adjustments + risk control only**.

## 2. What already exists — reuse, do not rebuild

- **Orders/Payments** (Phase 3): statuses already include
  `approved/rejected/delivered` and `approved/rejected`; `Order.admin_id`,
  `reject_reason`, `approved_at/rejected_at/delivered_at`; `Payment.admin_id`,
  `approved_at/rejected_at`, `tracking_code`. No model change needed for the core.
- **Wallet** — `user_service.adjust_wallet_balance(session, user_id, amount, *,
  reason, actor_type, actor_id, ip_address)` (signed amount; add = positive,
  subtract = negative) already writes a `WalletTransaction` + audit row.
  `user_service.list_wallet_transactions`, `admin_set_blocked`, `set_verified`
  also exist.
- **WalletTransaction** columns today: `user_id, amount, balance_after, reason,
  actor_type, actor_id, created_at`.
- **Permissions** vocab: `manage_users, adjust_wallet, manage_payments,
  approve_payments`; `can_*` helpers follow a fixed pattern.
- **Web**: `/admin/users/{id}` with POST `block/unblock/verify/note/wallet-adjust`;
  `/admin/orders`, `/admin/orders/pending-receipts`, `/admin/orders/{id}`,
  `/admin/receipts/{payment_id}`.
- **Bot**: admin panel commands; `app/bot/notifications.py` posts the receipt
  notice with a placeholder "next phase" inline button (`receipt_next_phase`).

## 3. Gaps to fill (each with a migration where noted)

**Migration 0011** (fresh + existing):
1. **User risk fields** — add `is_restricted` (bool, default false),
   `restriction_reason` (text, null), `restricted_until` (timestamptz, null).
2. **WalletTransaction** — add `balance_before` (bigint) and `type`
   (varchar, default `admin_adjustment`) to satisfy the required transaction
   shape. Treat the existing `actor_id` as the admin id and `reason` as the
   description (no duplicate columns).

Non-migration gaps:
3. **`app/services/wallet_service.py`** — thin, transactional wrapper over the
   existing adjustment: `adjust_balance(user_id, amount, admin_id, reason,
   transaction_type="admin_adjustment")`, `add_balance(...)`,
   `subtract_balance(...)`. Records `balance_before`/`balance_after`, `type`,
   `admin_id`, `reason`. Subtract refuses to drive the balance negative unless a
   new `allow_negative_wallet` setting is true.
4. **Permissions** — add `block_users`, `restrict_users`, and
   `process_payments` to the vocab (or map `process_payments` onto the existing
   `approve_payments` if we prefer no churn), plus helpers `can_block_users`,
   `can_restrict_users`, `can_process_payments`, `can_adjust_wallet` (exists).
   Role matrix:
   - **owner** — all.
   - **admin** — approve/reject, adjust wallet, block, restrict.
   - **accountant** — approve/reject, adjust wallet; **no** block/restrict.
   - **support** — view receipts, block, restrict; **no** approve, **no** wallet.
   - **viewer** — view only.
5. **Settings defaults** — add `restricted_user_text`
   (default fa: `«حساب شما محدود شده است. برای پیگیری با پشتیبانی تماس بگیرید.»`)
   and optional `allow_negative_wallet` (bool, default false).
6. **RestrictedMiddleware** (new, after `BlockedMiddleware`) — restricted,
   non-admin users cannot create orders, buy, submit receipts, or top up; they
   may still `/start`, `/ping`, view rules, and contact support. Shows
   `restricted_user_text`.

## 4. Web quick actions

New POST routes (all redirect back to the originating page with a flash; only
approve/reject change order/payment status):

| Action | Route | Permission |
|--------|-------|------------|
| Approve receipt | `POST /admin/orders/{id}/approve` | `process_payments` |
| Reject receipt (reason) | `POST /admin/orders/{id}/reject` | `process_payments` |
| Add balance (amount, reason) | `POST /admin/orders/{id}/add-balance` | `adjust_wallet` |
| Subtract balance (amount, reason) | `POST /admin/orders/{id}/subtract-balance` | `adjust_wallet` |
| Block user (reason optional) | `POST /admin/orders/{id}/block-user` | `block_users` |
| Restrict user (reason req, until opt) | `POST /admin/orders/{id}/restrict-user` | `restrict_users` |
| Unrestrict user | `POST /admin/orders/{id}/unrestrict-user` | `restrict_users` |
| View user | link to `/admin/users/{user_id}` | `view_users` |

- **Add/Subtract balance** modal fields: `amount` (>0), `reason` (required).
  Creates a `WalletTransaction`, writes an audit row, flashes success/error,
  and **stays on the order detail page**. Subtract prevents a negative balance
  unless `allow_negative_wallet`.
- **Order detail** and **pending-receipts** rows both expose these buttons.
- **`/admin/users/{id}`** additionally shows `is_blocked`, `is_restricted`,
  `restriction_reason`, `restricted_until`, wallet balance + adjustment history,
  related orders, and the same quick actions (add/subtract, block/unblock,
  restrict/unrestrict).

## 5. Telegram admin quick actions

Inline buttons on the receipt notification and in the pending-receipts view:
`✅ تأیید رسید · ❌ رد رسید · 💰 افزودن موجودی · ➖ کاهش موجودی · 🚫 بلاک کاربر ·
⚠️ محدود کردن کاربر · 👤 مشاهده کاربر · 🌐 مشاهده در پنل` (replaces the current
placeholder button).

FSM rules (hard requirements):
- State carries `order_id`, `user_id`, `admin_id`, `action`.
- **Only the admin who started an action may complete it.**
- `/cancel` and an inline «لغو» abort; expired/invalid states fail safely.
- Non-admin users cannot trigger these callbacks (guarded by role).

Flows:
- **Add balance**: ask amount → validate → ask reason → `wallet_service.add_balance`
  → «موجودی کاربر با موفقیت افزایش یافت.»
- **Subtract balance**: ask amount → validate (non-negative result) → ask reason →
  `wallet_service.subtract_balance` → success/error.
- **Block**: confirm «آیا مطمئن هستید…» → optional reason → `is_blocked=true` →
  audit → success; also notify the user «حساب شما توسط مدیریت مسدود شد.» if practical.
- **Restrict**: ask reason → optional `restricted_until` → `is_restricted=true` →
  audit → success; notify «حساب شما به‌صورت محدود شده است. دلیل: {reason}» if practical.

Blocking/restricting does **not** auto-reject the order — the admin must also tap
reject.

## 6. Audit actions (metadata: order_id, order_number, payment_id,
target_user_id, admin_id, amount, reason, previous_status, new_status — no secrets)

`admin_wallet_added_from_receipt_review`,
`admin_wallet_subtracted_from_receipt_review`,
`user_blocked_from_receipt_review`, `user_restricted_from_receipt_review`,
`user_unrestricted`, `failed_wallet_adjustment`, `failed_user_restriction`
(plus the core `payment_approved` / `payment_rejected` / `order_delivered`).

## 7. Tests

- **Web**: add-balance requires auth + permission; creates a WalletTransaction +
  audit; subtract prevents negative; block/restrict from order detail work;
  viewer cannot perform quick actions.
- **Telegram**: add-balance callback starts the FSM; amount validation; reason
  required; block requires confirmation; restrict stores reason; non-admin
  callback rejected; a different admin cannot complete another admin's FSM.
- **Middleware**: restricted user cannot create an order or submit a receipt;
  blocked user cannot use the normal flow.
- **Service**: wallet adjustment is transactional; restriction updates the User
  fields; audit rows are written.

## 8. Manual checklist

Order → receipt → open pending receipt → Add balance (amount + reason) → wallet
increases → transaction visible → audit created → Block user → user cannot use
bot → unblock from user detail → Restrict user → user can `/start` but cannot
order → Approve one receipt → Reject another with reason → duplicate
approve/reject still blocked.

## 9. Final Phase 4 report must answer

- Can admin add custom balance from receipt review?
- Can admin subtract custom balance from receipt review?
- Can admin block a user from receipt review?
- Can admin restrict a user from receipt review?
- What happens to restricted users in the bot?
- Are wallet-adjustment audit logs created?
- Are the Telegram admin quick actions working?
- Which roles can perform each action?

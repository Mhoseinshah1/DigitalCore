"""Telegram admin panel commands (owner/admin): stats, users, settings summary,
and quick user actions by Telegram ID.

Commands:
  /admin_stats                       — totals + maintenance/sales status
  /admin_users                       — recent users
  /admin_settings                    — key settings summary
  /admin_block <telegram_id>         — block a user
  /admin_unblock <telegram_id>       — unblock a user
  /admin_addbalance <tg_id> <amount> — credit a wallet (toman)
  /admin_subbalance <tg_id> <amount> — debit a wallet (toman)

Receipt approval / provisioning are NOT here (later phases). All state changes go
through the audited services.
"""
from __future__ import annotations

import logging
from collections.abc import Callable

from aiogram import F, Router
from aiogram.filters import Command, CommandObject, StateFilter
from aiogram.fsm.context import FSMContext
from aiogram.types import CallbackQuery, InlineKeyboardButton, InlineKeyboardMarkup, Message

from app.core.permissions import Role, has_permission
from app.core.settings_service import SettingsService
from app.database import SessionLocal
from app.i18n import texts_for
from app.services import product_service, user_service

log = logging.getLogger("bot.admin.panel")

router = Router(name="admin.panel")

# Reply-keyboard button texts (emoji + no-emoji + English + owner shorthand) that
# must each map to a handler — otherwise the button is "dead" (the owner's bug).
DASHBOARD_TEXTS = texts_for("btn.admin.dashboard") | {"داشبورد", "Dashboard"}
USERS_TEXTS = texts_for("btn.admin.users") | {"کاربران", "Users"}
# The old dead «اطلاع‌رسانی» button is repurposed as Financial; accept every label
# the owner might see, including their "اطلاعات ثانی" wording.
FINANCIAL_TEXTS = (
    texts_for("btn.admin.financial")
    | texts_for("btn.admin.broadcast")
    | {"اطلاعات مالی", "اطلاعات ثانی", "گزارش مالی", "مالی", "💰 اطلاعات مالی", "Financial"}
)
# Pending receipts stay a SEPARATE section from stats (owner's explicit request).
PENDING_TEXTS = (
    texts_for("btn.admin.pending")
    | {"رسیدهای تایید نشده", "🧾 رسیدهای تایید نشده", "رسیدهای در انتظار", "Pending receipts"}
)

CB_ADMIN_PENDING = "adm:pending"  # inline quick-jump to pending receipts


async def build_overview(lang: str, _: Callable[..., str]) -> str:
    """Stats + status summary shown by /admin and /admin_stats."""
    async with SessionLocal() as session:
        stats = await user_service.get_stats(session)
        products = await product_service.list_for_user(session)
        svc = SettingsService(session)
        maintenance = await svc.get_bool("maintenance_mode", False)
        sales = await svc.get_bool("sales_enabled", True)
    on, off = _("admin.stats.on"), _("admin.stats.off")
    return "\n".join([
        _("admin.stats.title"),
        "",
        _("admin.stats.users", n=stats["total_users"]),
        _("admin.stats.blocked", n=stats["blocked_users"]),
        _("admin.stats.products", n=len(products)),
        _("admin.stats.maintenance", state=on if maintenance else off),
        _("admin.stats.sales", state=on if sales else off),
    ])


@router.message(Command("admin_stats"))
async def on_admin_stats(
    message: Message, _: Callable[..., str], lang: str = "fa", role: Role | None = None
) -> None:
    if not has_permission(role, "view_dashboard"):
        await message.answer(_("admin.not_authorized"))
        return
    await message.answer(await build_overview(lang, _), parse_mode="HTML")


@router.message(Command("admin_licenses"))
@router.message(Command("admin_license_stock"))
async def on_admin_licenses(
    message: Message, _: Callable[..., str], lang: str = "fa", role: Role | None = None
) -> None:
    if not has_permission(role, "view_licenses"):
        await message.answer(_("admin.not_authorized"))
        return
    from app.services import license_service, product_service
    async with SessionLocal() as session:
        products = [p for p in await product_service.list_for_admin(session)
                    if p.type == "license"]
        threshold = await SettingsService(session).get_int("license_low_stock_threshold", 5)
        rows = []
        for p in products:
            counts = await license_service.count_by_status(session, p.id)
            rows.append((p, counts.get("available", 0), counts.get("sold", 0)))
    if not rows:
        await message.answer(_("admin.licenses.none"))
        return
    lines = [_("admin.licenses.title"), ""]
    for p, avail, sold in rows:
        warn = " ⚠️" if avail < threshold else ""
        lines.append(_("admin.licenses.row", title=p.title, avail=avail, sold=sold) + warn)
    lines += ["", _("admin.licenses.threshold", n=threshold)]
    await message.answer("\n".join(lines), parse_mode="HTML")


@router.message(Command("admin_v2ray"))
async def on_admin_v2ray(
    message: Message, _: Callable[..., str], lang: str = "fa", role: Role | None = None
) -> None:
    if not has_permission(role, "view_services"):
        await message.answer(_("admin.not_authorized"))
        return
    from app.services import v2ray_service
    async with SessionLocal() as session:
        counts = await v2ray_service.count_by_status(session)
    total = sum(counts.values())
    if total == 0:
        await message.answer(_("admin.services.none"))
        return
    await message.answer("\n".join([
        _("admin.services.title"), "",
        _("admin.services.counts", active=counts.get("active", 0),
          failed=counts.get("failed", 0), total=total),
    ]), parse_mode="HTML")


@router.message(Command("admin_v2ray_failed"))
async def on_admin_v2ray_failed(
    message: Message, _: Callable[..., str], lang: str = "fa", role: Role | None = None
) -> None:
    if not has_permission(role, "view_services"):
        await message.answer(_("admin.not_authorized"))
        return
    from app.services import v2ray_service
    async with SessionLocal() as session:
        failed = await v2ray_service.list_services(session, status="failed", limit=20)
        rows = [((s.order.order_number if s.order else f"#{s.order_id}"), s.last_error or "—")
                for s in failed]
    if not rows:
        await message.answer(_("admin.services.failed_none"))
        return
    lines = [_("admin.services.failed_title"), ""]
    for number, err in rows:
        lines.append(_("admin.services.failed_row", number=number, error=err))
    await message.answer("\n".join(lines), parse_mode="HTML")


@router.message(Command("admin_users"))
async def on_admin_users(
    message: Message, _: Callable[..., str], lang: str = "fa", role: Role | None = None
) -> None:
    if not has_permission(role, "view_users"):
        await message.answer(_("admin.not_authorized"))
        return
    async with SessionLocal() as session:
        users = await user_service.list_users(session, limit=15)
    if not users:
        await message.answer(_("admin.users.none"))
        return
    lines = [_("admin.users.title"), ""]
    for u in users:
        name = ("@" + u.username) if u.username else (str(u.telegram_id) or f"#{u.id}")
        flag = " 🚫" if u.is_blocked else ""
        lines.append(_("admin.users.line", name=name, wallet=f"{u.wallet_balance or 0:,}") + flag)
    await message.answer("\n".join(lines))


@router.message(Command("admin_settings"))
async def on_admin_settings(
    message: Message, _: Callable[..., str], lang: str = "fa", role: Role | None = None
) -> None:
    if not has_permission(role, "manage_settings"):
        await message.answer(_("admin.not_authorized"))
        return
    async with SessionLocal() as session:
        svc = SettingsService(session)
        site = await svc.get_str("site_name", "DigitalCore")
        maintenance = await svc.get_bool("maintenance_mode", False)
        sales = await svc.get_bool("sales_enabled", True)
        wallet = await svc.get_bool("wallet_enabled", True)
        c2c = await svc.get_bool("card_to_card_enabled", True)
    on, off = _("admin.stats.on"), _("admin.stats.off")
    lines = [
        _("admin.settings.title"),
        "",
        _("admin.settings.site", value=site),
        _("admin.settings.maintenance", state=on if maintenance else off),
        _("admin.settings.sales", state=on if sales else off),
        _("admin.settings.wallet", state=on if wallet else off),
        _("admin.settings.c2c", state=on if c2c else off),
    ]
    await message.answer("\n".join(lines))


def _parse_int(text: str | None) -> int | None:
    try:
        return int((text or "").strip().replace(",", ""))
    except (TypeError, ValueError):
        return None


async def _set_blocked(message: Message, command: CommandObject, _, role, blocked: bool) -> None:
    if not has_permission(role, "manage_users"):
        await message.answer(_("admin.not_authorized"))
        return
    tg_id = _parse_int(command.args)
    if tg_id is None:
        await message.answer(_("admin.action.usage_id"))
        return
    actor = message.from_user.id if message.from_user else None
    async with SessionLocal() as session:
        user = await user_service.get_by_telegram_id(session, tg_id)
        if user is None:
            await message.answer(_("admin.action.user_not_found", id=tg_id))
            return
        await user_service.admin_set_blocked(session, user.id, blocked, actor_id=actor)
        await session.commit()
    await message.answer(
        _("admin.action.blocked_ok" if blocked else "admin.action.unblocked_ok", id=tg_id)
    )


@router.message(Command("admin_block"))
async def on_admin_block(
    message: Message, command: CommandObject, _: Callable[..., str], role: Role | None = None
) -> None:
    await _set_blocked(message, command, _, role, True)


@router.message(Command("admin_unblock"))
async def on_admin_unblock(
    message: Message, command: CommandObject, _: Callable[..., str], role: Role | None = None
) -> None:
    await _set_blocked(message, command, _, role, False)


async def _adjust(message: Message, command: CommandObject, _, role, sign: int) -> None:
    if not has_permission(role, "adjust_wallet"):
        await message.answer(_("admin.not_authorized"))
        return
    parts = (command.args or "").split()
    if len(parts) != 2:
        await message.answer(_("admin.action.usage_balance"))
        return
    tg_id = _parse_int(parts[0])
    amount = _parse_int(parts[1])
    if tg_id is None or amount is None or amount <= 0:
        await message.answer(_("admin.action.bad_amount"))
        return
    actor = message.from_user.id if message.from_user else None
    async with SessionLocal() as session:
        user = await user_service.get_by_telegram_id(session, tg_id)
        if user is None:
            await message.answer(_("admin.action.user_not_found", id=tg_id))
            return
        try:
            user = await user_service.adjust_wallet_balance(
                session, user.id, sign * amount, reason="telegram-admin",
                actor_type="admin", actor_id=actor,
            )
            await session.commit()
        except ValueError as exc:
            await message.answer(_("admin.action.balance_error", error=str(exc)))
            return
    await message.answer(
        _("admin.action.balance_ok", id=tg_id, balance=f"{user.wallet_balance:,}")
    )


@router.message(Command("admin_addbalance"))
async def on_admin_addbalance(
    message: Message, command: CommandObject, _: Callable[..., str], role: Role | None = None
) -> None:
    await _adjust(message, command, _, role, +1)


@router.message(Command("admin_subbalance"))
async def on_admin_subbalance(
    message: Message, command: CommandObject, _: Callable[..., str], role: Role | None = None
) -> None:
    await _adjust(message, command, _, role, -1)


# ==========================================================================
# Reply-keyboard button handlers (previously dead: dashboard / users /
# financial). Each responds to the emoji + plain + English label variants.
# ==========================================================================
async def build_dashboard(_: "Callable[..., str]") -> str:
    """Rich admin dashboard. Degrades gracefully — a query failure still renders."""
    from app.services import report_service
    lines = [_("admin.dash.title"), ""]
    async with SessionLocal() as session:
        try:
            stats = await user_service.get_stats(session)
            lines.append(_("admin.dash.users", n=stats.get("total_users", 0)))
        except Exception as exc:  # noqa: BLE001
            log.warning("dashboard users failed: %s", exc)
            lines.append(_("admin.dash.users", n="—"))
        try:
            start, end = report_service.parse_date_range(preset="last_30_days")
            s = await report_service.get_dashboard_summary(session, start, end)
        except Exception as exc:  # noqa: BLE001 - dashboard must still render
            log.warning("dashboard summary failed: %s", exc)
            s = None
    if s:
        att = s.get("attention", {})
        v2 = s.get("v2ray", {}).get("by_status", {})
        lines += [
            _("admin.dash.pending_receipts", n=att.get("pending_receipts", 0)),
            _("admin.dash.pending_topups", n=att.get("pending_topups", 0)),
            _("admin.dash.failed_orders", n=att.get("failed_orders", 0)),
            _("admin.dash.active_services", n=v2.get("active", 0)),
            _("admin.dash.low_stock", n=att.get("low_stock_products", 0)),
            _("admin.dash.v2ray_failed", n=att.get("v2ray_failed", 0)),
            _("admin.dash.revenue", amount=f"{s.get('revenue', {}).get('total', 0):,}"),
        ]
    else:
        lines.append(_("admin.dash.stats_unavailable"))
    return "\n".join(lines)


def _pending_kb(_: "Callable[..., str]") -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(inline_keyboard=[[
        InlineKeyboardButton(text=_("admin.dash.btn.pending"),
                             callback_data=CB_ADMIN_PENDING)]])


# StateFilter(None): a reply-menu button must never hijack an in-progress FSM
# edit (settings/products/…) — those routers load after this one, so gate on "no
# active flow" to stay out of their way.
@router.message(StateFilter(None), F.text.in_(DASHBOARD_TEXTS))
async def on_admin_dashboard(
    message: Message, _: "Callable[..., str]", lang: str = "fa", role: Role | None = None
) -> None:
    if role is None:  # ordinary user typed a matching word → let it fall through
        return
    if not has_permission(role, "view_dashboard"):
        await message.answer(_("admin.not_authorized"))
        return
    await message.answer(await build_dashboard(_), parse_mode="HTML",
                         reply_markup=_pending_kb(_))


@router.message(StateFilter(None), F.text.in_(USERS_TEXTS))
async def on_admin_users_button(
    message: Message, _: "Callable[..., str]", lang: str = "fa", role: Role | None = None
) -> None:
    if role is None:  # ordinary user typed a matching word → let it fall through
        return
    if not has_permission(role, "view_users"):
        await message.answer(_("admin.not_authorized"))
        return
    async with SessionLocal() as session:
        try:
            stats = await user_service.get_stats(session)
        except Exception as exc:  # noqa: BLE001
            log.warning("admin users stats failed: %s", exc)
            stats = {}
        users = await user_service.list_users(session, limit=10)
    lines = [
        _("admin.users_panel.title"), "",
        _("admin.users_panel.total", n=stats.get("total_users", 0)),
        _("admin.users_panel.verified", n=stats.get("verified_users", 0)),
        _("admin.users_panel.blocked", n=stats.get("blocked_users", 0)),
        "",
        _("admin.users_panel.recent"),
    ]
    for u in users:
        name = ("@" + u.username) if u.username else str(u.telegram_id)
        flag = " 🚫" if u.is_blocked else ""
        lines.append(_("admin.users.line", name=name,
                       wallet=f"{u.wallet_balance or 0:,}") + flag)
    lines += ["", _("admin.users_panel.search_hint")]
    await message.answer("\n".join(lines), parse_mode="HTML")


async def build_financial(_: "Callable[..., str]") -> str:
    """Financial snapshot: paid today, pending receipts/top-ups, paid/rejected."""
    from datetime import datetime, timezone
    from sqlalchemy import func, select
    from app.models.payment import Payment
    from app.models.wallet_topup import WalletTopupRequest
    lines = [_("admin.fin.title"), ""]
    async with SessionLocal() as session:
        async def _count(model, *where):
            try:
                return int(await session.scalar(
                    select(func.count(model.id)).where(*where)) or 0)
            except Exception as exc:  # noqa: BLE001
                log.warning("financial count failed: %s", exc)
                return "—"
        today = datetime.now(timezone.utc).date()
        try:
            paid_today = int(await session.scalar(
                select(func.coalesce(func.sum(Payment.amount), 0)).where(
                    Payment.status == "approved",
                    func.date(Payment.approved_at) == today)) or 0)
        except Exception as exc:  # noqa: BLE001
            log.warning("financial paid_today failed: %s", exc)
            paid_today = 0
        pending_receipts = await _count(Payment, Payment.status == "receipt_submitted")
        pending_topups = await _count(WalletTopupRequest,
                                      WalletTopupRequest.status == "waiting_admin")
        paid = await _count(Payment, Payment.status == "approved")
        rejected = await _count(Payment, Payment.status == "rejected")
    lines += [
        _("admin.fin.paid_today", amount=f"{paid_today:,}"),
        _("admin.fin.pending_receipts", n=pending_receipts),
        _("admin.fin.pending_topups", n=pending_topups),
        _("admin.fin.paid_count", n=paid),
        _("admin.fin.rejected_count", n=rejected),
    ]
    return "\n".join(lines)


@router.message(StateFilter(None), F.text.in_(FINANCIAL_TEXTS))
async def on_admin_financial(
    message: Message, _: "Callable[..., str]", lang: str = "fa", role: Role | None = None
) -> None:
    if role is None:  # ordinary user typed a matching word → let it fall through
        return
    if not has_permission(role, "view_payments"):
        await message.answer(_("admin.not_authorized"))
        return
    await message.answer(await build_financial(_), parse_mode="HTML",
                         reply_markup=_pending_kb(_))


async def _render_pending_receipts(_: "Callable[..., str]") -> str:
    from app.models.user import User
    from app.services import payment_core_service
    async with SessionLocal() as session:
        payments = await payment_core_service.list_payments(
            session, status="receipt_submitted", limit=20)
        rows = []
        for p in payments:
            u = await session.get(User, p.user_id)
            label = ("@" + u.username) if (u and u.username) else (
                str(u.telegram_id) if u else f"#{p.user_id}")
            rows.append((p.tracking_code or f"#{p.id}", int(p.amount or 0), label))
    if not rows:
        return _("admin.fin.pending_empty")
    lines = [_("admin.fin.pending_title"), ""]
    for code, amount, who in rows:
        lines.append(_("admin.fin.pending_row", code=code, amount=f"{amount:,}", who=who))
    lines += ["", _("admin.fin.pending_web_hint")]
    return "\n".join(lines)


@router.callback_query(F.data == CB_ADMIN_PENDING)
async def on_admin_pending_cb(
    callback: CallbackQuery, _: "Callable[..., str]", role: Role | None = None
) -> None:
    if not has_permission(role, "view_payments"):
        await callback.answer(_("admin.not_authorized"), show_alert=True)
        return
    await callback.answer()
    if callback.message is not None:
        await callback.message.answer(await _render_pending_receipts(_), parse_mode="HTML")


@router.message(StateFilter(None), F.text.in_(PENDING_TEXTS))
async def on_admin_pending_button(
    message: Message, _: "Callable[..., str]", lang: str = "fa", role: Role | None = None
) -> None:
    """«🧾 رسیدهای تایید نشده» — its own admin section, kept separate from stats."""
    if role is None:  # ordinary user typed a matching word → let it fall through
        return
    if not has_permission(role, "view_payments"):
        await message.answer(_("admin.not_authorized"))
        return
    await message.answer(await _render_pending_receipts(_), parse_mode="HTML")


@router.message(Command("admin_debug"))
async def on_admin_debug(
    message: Message, _: "Callable[..., str]", state: FSMContext,
    lang: str = "fa", role: Role | None = None,
) -> None:
    """Owner/admin runtime diagnostic (safe: no tokens/secrets)."""
    if not has_permission(role, "view_dashboard"):
        await message.answer(_("admin.not_authorized"))
        return
    from app import __version__
    from app.services import payment_core_service, payment_service
    fsm_state = await state.get_state()
    async with SessionLocal() as session:
        methods = await payment_core_service.list_methods(session)
        active = [m for m in methods if m.is_active]
        try:
            from sqlalchemy import func, select
            from app.models.payment import Payment
            pending_pay = int(await session.scalar(
                select(func.count(Payment.id)).where(Payment.status == "pending")) or 0)
            pending_receipts = int(await session.scalar(
                select(func.count(Payment.id)).where(
                    Payment.status == "receipt_submitted")) or 0)
        except Exception as exc:  # noqa: BLE001
            pending_pay = pending_receipts = f"err:{type(exc).__name__}"
    # Storage writability probe (never raises).
    storage = payment_service.RECEIPTS_ROOT
    writable = False
    try:
        storage.mkdir(parents=True, exist_ok=True)
        probe = storage / ".write_probe"
        probe.write_text("ok")
        probe.unlink(missing_ok=True)
        writable = True
    except Exception:  # noqa: BLE001
        writable = False
    lines = [
        "🛠 <b>admin debug</b>",
        f"version: {__version__}",
        f"role: {role.value if role else '-'}",
        f"fsm_state: {fsm_state or 'None'}",
        f"active payment methods: {len(active)}/{len(methods)}",
        "  " + (", ".join(f"{m.code}({m.method_type})" for m in active) or "-"),
        f"pending payments: {pending_pay}",
        f"pending receipts: {pending_receipts}",
        f"storage: {storage}",
        f"storage writable: {writable}",
        f"receipt state handlers: registered",
        "admin menu: " + ", ".join(sorted(DASHBOARD_TEXTS | USERS_TEXTS)[:1]) + " …",
    ]
    await message.answer("<pre>" + "\n".join(lines) + "</pre>", parse_mode="HTML")

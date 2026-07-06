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

from aiogram import Router
from aiogram.filters import Command, CommandObject
from aiogram.types import Message

from app.core.permissions import Role, has_permission
from app.core.settings_service import SettingsService
from app.database import SessionLocal
from app.services import product_service, user_service

log = logging.getLogger("bot.admin.panel")

router = Router(name="admin.panel")


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

"""Telegram admin '📊 آمار ربات' section.

A read-only, RBAC-gated statistics panel inside the bot admin menu: an overview
message with a range switcher (all / last hour / today / yesterday / this month
/ last month / custom Jalali range) and a product-sales comparison. Figures come
from :mod:`app.services.bot_stats_service` (efficient SQL aggregates).

Pending receipts are a SEPARATE section (see admin/panel.py) — deliberately not
merged here, per the owner's request.
"""
from __future__ import annotations

import logging
from collections.abc import Callable
from datetime import datetime

from aiogram import F, Router
from aiogram.filters import StateFilter
from aiogram.fsm.context import FSMContext
from aiogram.fsm.state import State, StatesGroup
from aiogram.types import (
    CallbackQuery,
    InlineKeyboardButton,
    InlineKeyboardMarkup,
    Message,
)

from app.bot.keyboards.admin import admin_main_menu
from app.core.permissions import Role, has_permission
from app.database import SessionLocal
from app.i18n import menu_texts, texts_for
from app.services import bot_stats_service

log = logging.getLogger("bot.admin.stats")

router = Router(name="admin.stats")

# The reply-keyboard button + tolerant text variants (incl. the old dashboard
# labels, so a cached keyboard still lands here).
STATS_TEXTS: set[str] = (
    menu_texts("btn.admin.stats")
    | {"📊 آمار ربات", "آمار ربات", "داشبورد", "📊 داشبورد", "Stats", "Dashboard"}
)

CB_STATS = "admin_stats:"                 # admin_stats:<range|custom|products>
CB_STATS_PRODUCTS = "admin_stats_products:"  # admin_stats_products:<7d|30d|current_month|custom>
CB_MENU_BACK = "admin_menu:back"

# Range presets shown as inline buttons (order matters).
_RANGE_BUTTONS: tuple[str, ...] = (
    "all", "last_hour", "today", "yesterday", "current_month", "previous_month",
)
# Product-comparison preset -> the range_type understood by the stats service.
_PRODUCT_PRESETS: dict[str, str] = {
    "7d": "last_7_days", "30d": "last_30_days", "current_month": "this_month",
}


class StatsStates(StatesGroup):
    waiting_stats_start_date = State()
    waiting_stats_end_date = State()
    waiting_products_start_date = State()
    waiting_products_end_date = State()


def _allowed(role: Role | None) -> bool:
    # Stats include financial totals, so gate on view_payments: owner / admin /
    # accountant may see them; support / viewer may not.
    return role is not None and has_permission(role, "view_payments")


def _fmt(n) -> str:
    return f"{int(n or 0):,}"


def _pct(x) -> str:
    return f"{float(x or 0):g}"


# ---------------------------------------------------------------------------
# Message + keyboard building
# ---------------------------------------------------------------------------
def _stats_keyboard(_: Callable[..., str]) -> InlineKeyboardMarkup:
    def b(key: str, data: str) -> InlineKeyboardButton:
        return InlineKeyboardButton(text=_(key), callback_data=data)
    return InlineKeyboardMarkup(inline_keyboard=[
        [b("admin.stats.btn.all", f"{CB_STATS}all"),
         b("admin.stats.btn.last_hour", f"{CB_STATS}last_hour")],
        [b("admin.stats.btn.today", f"{CB_STATS}today"),
         b("admin.stats.btn.yesterday", f"{CB_STATS}yesterday")],
        [b("admin.stats.btn.current_month", f"{CB_STATS}current_month"),
         b("admin.stats.btn.previous_month", f"{CB_STATS}previous_month")],
        [b("admin.stats.btn.custom", f"{CB_STATS}custom")],
        [b("admin.stats.btn.products", f"{CB_STATS}products")],
        [b("admin.stats.btn.back", CB_MENU_BACK)],
    ])


def _products_keyboard(_: Callable[..., str]) -> InlineKeyboardMarkup:
    def b(key: str, data: str) -> InlineKeyboardButton:
        return InlineKeyboardButton(text=_(key), callback_data=data)
    return InlineKeyboardMarkup(inline_keyboard=[
        [b("admin.stats.products.btn.7d", f"{CB_STATS_PRODUCTS}7d"),
         b("admin.stats.products.btn.30d", f"{CB_STATS_PRODUCTS}30d")],
        [b("admin.stats.products.btn.current_month", f"{CB_STATS_PRODUCTS}current_month")],
        [b("admin.stats.products.btn.custom", f"{CB_STATS_PRODUCTS}custom")],
        [b("admin.stats.products.btn.back", f"{CB_STATS}all")],
    ])


def _range_title(range_type: str, _: Callable[..., str],
                 start: datetime | None = None, end: datetime | None = None) -> str:
    if range_type == "custom" and start is not None and end is not None:
        # end is the exclusive next-midnight; show the inclusive last day.
        from datetime import timedelta
        last = (end - timedelta(days=1)).strftime("%Y-%m-%d")
        return _("admin.stats.range.custom", start=start.strftime("%Y-%m-%d"), end=last)
    key = f"admin.stats.range.{range_type}"
    title = _(key)
    return title if title != key else range_type


def build_stats_message(stats: dict, gateways: list[dict], range_title: str,
                        _: Callable[..., str]) -> str:
    body = _(
        "admin.stats.body",
        range_title=range_title,
        total_users=_fmt(stats["total_users"]),
        users_with_purchase=_fmt(stats["users_with_purchase"]),
        total_test_accounts=_fmt(stats["total_test_accounts"]),
        total_users_balance=_fmt(stats["total_users_balance"]),
        total_sales_count=_fmt(stats["total_sales_count"]),
        active_services_sales_count=_fmt(stats["active_services_sales_count"]),
        total_sales_amount=_fmt(stats["total_sales_amount"]),
        active_services_sales_amount=_fmt(stats["active_services_sales_amount"]),
        total_renew_amount=_fmt(stats["total_renew_amount"]),
        total_discount_amount=_fmt(stats["total_discount_amount"]),
        discount_usage_count=_fmt(stats["discount_usage_count"]),
        conversion_rate=_pct(stats["conversion_rate"]),
        average_purchase_per_customer=_fmt(stats["average_purchase_per_customer"]),
        predicted_monthly_income=_fmt(stats["predicted_monthly_income"]),
        renew_percent_from_sales=_pct(stats["renew_percent_from_sales"]),
        total_resellers=_fmt(stats["total_resellers"]),
        n_resellers_count=_fmt(stats["n_resellers_count"]),
        n2_resellers_count=_fmt(stats["n2_resellers_count"]),
        total_panels=_fmt(stats["total_panels"]),
    )
    parts = [body, ""]
    if gateways:
        for g in gateways:
            parts.append(_("admin.stats.gateway_row",
                           name=g["gateway_name"],
                           count=_fmt(g["successful_payments_count"]),
                           amount=_fmt(g["successful_payments_amount"])))
    else:
        parts.append(_("admin.stats.no_gateways"))
    return "\n".join(parts)


async def _load_stats_text(range_type: str, _: Callable[..., str], *,
                           start_at: datetime | None = None,
                           end_at: datetime | None = None) -> str:
    async with SessionLocal() as session:
        start, end = bot_stats_service.calculate_range(
            range_type, start_at=start_at, end_at=end_at)
        stats = await bot_stats_service.get_bot_stats(
            session, range_type, start_at=start_at, end_at=end_at)
        gateways = await bot_stats_service.get_gateway_stats(session, start, end)
    title = _range_title(range_type, _, start, end)
    return build_stats_message(stats, gateways, title, _)


async def _edit_or_send(callback: CallbackQuery, text: str,
                        kb: InlineKeyboardMarkup) -> None:
    msg = callback.message
    if msg is None:
        return
    try:
        await msg.edit_text(text, parse_mode="HTML", reply_markup=kb)
    except Exception:  # noqa: BLE001 - message unchanged / too old / not editable
        await msg.answer(text, parse_mode="HTML", reply_markup=kb)


# ---------------------------------------------------------------------------
# Entry: reply-keyboard button
# ---------------------------------------------------------------------------
@router.message(StateFilter(None), F.text.in_(STATS_TEXTS))
async def on_stats_button(
    message: Message, _: Callable[..., str], state: FSMContext,
    lang: str = "fa", role: Role | None = None,
) -> None:
    if role is None:  # ordinary user typed a matching word → fall through
        return
    if not _allowed(role):
        await message.answer(_("admin.stats.denied"))
        return
    text = await _load_stats_text("all", _)
    await message.answer(text, parse_mode="HTML", reply_markup=_stats_keyboard(_))


# ---------------------------------------------------------------------------
# Range switcher + custom / products entry (callbacks)
# ---------------------------------------------------------------------------
@router.callback_query(F.data.startswith(CB_STATS))
async def on_stats_cb(
    callback: CallbackQuery, _: Callable[..., str], state: FSMContext,
    lang: str = "fa", role: Role | None = None,
) -> None:
    if not _allowed(role):
        await callback.answer(_("admin.stats.denied"), show_alert=True)
        return
    what = (callback.data or "")[len(CB_STATS):]

    if what == "custom":
        await state.set_state(StatsStates.waiting_stats_start_date)
        await callback.answer()
        if callback.message is not None:
            await callback.message.answer(_("admin.stats.ask_start"))
        return

    if what == "products":
        await state.set_state(None)
        await callback.answer()
        await _edit_or_send(callback, _("admin.stats.products.menu"), _products_keyboard(_))
        return

    # A preset range → re-render the overview in place.
    range_type = what if what in _RANGE_BUTTONS else "all"
    await state.set_state(None)
    text = await _load_stats_text(range_type, _)
    await _edit_or_send(callback, text, _stats_keyboard(_))
    await callback.answer()


@router.callback_query(F.data == CB_MENU_BACK)
async def on_stats_back(
    callback: CallbackQuery, _: Callable[..., str], state: FSMContext,
    lang: str = "fa", role: Role | None = None,
) -> None:
    await state.set_state(None)
    await callback.answer()
    if callback.message is not None:
        await callback.message.answer(
            _("admin.stats.back_to_menu"), reply_markup=admin_main_menu(lang))


# ---------------------------------------------------------------------------
# Custom date range (stats)
# ---------------------------------------------------------------------------
@router.message(StatsStates.waiting_stats_start_date, F.text)
async def on_stats_start_date(
    message: Message, _: Callable[..., str], state: FSMContext, lang: str = "fa",
) -> None:
    start = bot_stats_service.parse_jalali_date(message.text or "")
    if start is None:
        await message.answer(_("admin.stats.bad_date"))
        return
    await state.update_data(stats_start=start.isoformat())
    await state.set_state(StatsStates.waiting_stats_end_date)
    await message.answer(_("admin.stats.ask_end"))


@router.message(StatsStates.waiting_stats_end_date, F.text)
async def on_stats_end_date(
    message: Message, _: Callable[..., str], state: FSMContext, lang: str = "fa",
) -> None:
    end = bot_stats_service.parse_jalali_date(message.text or "", end_of_day=True)
    if end is None:
        await message.answer(_("admin.stats.bad_date"))
        return
    data = await state.get_data()
    start = datetime.fromisoformat(data["stats_start"]) if data.get("stats_start") else None
    if start is not None and end <= start:
        await state.set_state(StatsStates.waiting_stats_start_date)
        await message.answer(_("admin.stats.end_before_start"))
        return
    await state.clear()
    text = await _load_stats_text("custom", _, start_at=start, end_at=end)
    await message.answer(text, parse_mode="HTML", reply_markup=_stats_keyboard(_))


# ---------------------------------------------------------------------------
# Product-sales comparison
# ---------------------------------------------------------------------------
def _products_range_title(preset: str, _: Callable[..., str],
                          start: datetime | None = None, end: datetime | None = None) -> str:
    key = f"admin.stats.products.range.{preset}"
    title = _(key)
    if title != key:
        return title
    if start is not None and end is not None:
        from datetime import timedelta
        return _("admin.stats.range.custom", start=start.strftime("%Y-%m-%d"),
                 end=(end - timedelta(days=1)).strftime("%Y-%m-%d"))
    return preset


def build_products_message(rows: list[dict], range_title: str,
                           _: Callable[..., str]) -> str:
    parts = [_("admin.stats.products.title", range_title=range_title)]
    if not rows:
        parts.append("")
        parts.append(_("admin.stats.products.empty"))
        return "\n".join(parts)
    for r in rows:
        parts.append(_("admin.stats.products.row", name=r["product_name"],
                       count=_fmt(r["sales_count"]), amount=_fmt(r["sales_amount"])))
    return "\n".join(parts)


async def _load_products_text(preset: str, _: Callable[..., str], *,
                              start_at: datetime | None = None,
                              end_at: datetime | None = None) -> str:
    if preset == "custom":
        start, end = start_at, end_at
        title = _products_range_title("custom", _, start, end)
    else:
        range_type = _PRODUCT_PRESETS.get(preset, "last_7_days")
        start, end = bot_stats_service.calculate_range(range_type)
        title = _products_range_title(preset, _, start, end)
    async with SessionLocal() as session:
        rows = await bot_stats_service.get_product_sales_comparison(session, start, end)
    return build_products_message(rows, title, _)


@router.callback_query(F.data.startswith(CB_STATS_PRODUCTS))
async def on_products_cb(
    callback: CallbackQuery, _: Callable[..., str], state: FSMContext,
    lang: str = "fa", role: Role | None = None,
) -> None:
    if not _allowed(role):
        await callback.answer(_("admin.stats.denied"), show_alert=True)
        return
    preset = (callback.data or "")[len(CB_STATS_PRODUCTS):]
    if preset == "custom":
        await state.set_state(StatsStates.waiting_products_start_date)
        await callback.answer()
        if callback.message is not None:
            await callback.message.answer(_("admin.stats.ask_start"))
        return
    await state.set_state(None)
    text = await _load_products_text(preset, _)
    await _edit_or_send(callback, text, _products_keyboard(_))
    await callback.answer()


@router.message(StatsStates.waiting_products_start_date, F.text)
async def on_products_start_date(
    message: Message, _: Callable[..., str], state: FSMContext, lang: str = "fa",
) -> None:
    start = bot_stats_service.parse_jalali_date(message.text or "")
    if start is None:
        await message.answer(_("admin.stats.bad_date"))
        return
    await state.update_data(products_start=start.isoformat())
    await state.set_state(StatsStates.waiting_products_end_date)
    await message.answer(_("admin.stats.ask_end"))


@router.message(StatsStates.waiting_products_end_date, F.text)
async def on_products_end_date(
    message: Message, _: Callable[..., str], state: FSMContext, lang: str = "fa",
) -> None:
    end = bot_stats_service.parse_jalali_date(message.text or "", end_of_day=True)
    if end is None:
        await message.answer(_("admin.stats.bad_date"))
        return
    data = await state.get_data()
    start = datetime.fromisoformat(data["products_start"]) if data.get("products_start") else None
    if start is not None and end <= start:
        await state.set_state(StatsStates.waiting_products_start_date)
        await message.answer(_("admin.stats.end_before_start"))
        return
    await state.clear()
    text = await _load_products_text("custom", _, start_at=start, end_at=end)
    await message.answer(text, parse_mode="HTML", reply_markup=_products_keyboard(_))

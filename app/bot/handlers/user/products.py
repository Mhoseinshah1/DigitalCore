"""User product browsing (bot UX): categories → products → invoice (pre-factor).

Three steps:
  1. category picker  (``on_products`` → ``render_categories``)
  2. products in a category  (``CB_CAT`` → ``render_category_products``)
  3. product detail rendered as an invoice with payment buttons  (``CB_DETAIL``)

If no active categories have products, the single "سایر محصولات" (Other) group is
shown directly as a flat product list so an operator that never uses categories
keeps the old flat experience. Buying is owned by orders.py (the payment buttons
on the invoice route into its coupon/payment flow).
"""
from __future__ import annotations

from collections.abc import Callable

from aiogram import F, Router
from aiogram.filters import Command
from aiogram.fsm.context import FSMContext
from aiogram.types import CallbackQuery, InlineKeyboardButton, InlineKeyboardMarkup, Message

from app.bot.utils.message_format import esc, format_gb, format_money, render_big_message
from app.database import SessionLocal
from app.i18n import menu_texts, t
from app.models.product import Product
from app.services import product_category_service, product_service, xui_server_service

router = Router(name="user.products")

CB_CATS = "ucats"       # step 1: category picker (also "back to categories")
CB_CAT = "ucat:"        # step 2: products in a category; payload = <id> or "none"
CB_DETAIL = "uprod:"    # step 3: product detail + invoice
CB_LIST = "uprods"      # legacy alias kept for older inline messages → step 1


def _cat_payload(category_id: int | None) -> str:
    return "none" if category_id is None else str(category_id)


def _parse_cat_payload(raw: str) -> int | None:
    return None if raw == "none" else int(raw)


def _price_text(product: Product, lang: str) -> str:
    return t("product.price_fmt", lang, price=f"{product.price:,}")


def build_detail_lines(product: Product, server_name: str | None, lang: str) -> list[str]:
    """The user-facing product detail. Renders only safe, presentational fields —
    never a server's base_url/username or the panel-side inbound id."""
    lines = [f"<b>{product.title}</b>", ""]
    lines.append(f"{t(f'product.type.{product.type}', lang)} · {_price_text(product, lang)}")
    if product.type == "v2ray":
        lines.append(
            f"{t('product.duration_fmt', lang, days=product.duration_days)} · "
            f"{t('product.traffic_fmt', lang, gb=product.traffic_gb)}"
        )
        if product.ip_limit:
            lines.append(t("product.ip_limit_fmt", lang, n=product.ip_limit))
        if server_name:
            lines.append(t("product.server_fmt", lang, name=server_name))
    if product.description:
        lines.extend(["", product.description])
    return lines


def build_invoice_lines(
    product: Product, category_title: str, server_name: str | None, lang: str
) -> list[str]:
    """The step-3 pre-factor (پیش‌فاکتور): a large, labelled order summary.

    Rendered in the shared "big message" style — a title, a divider, then each
    label on its own line above its value — so it reads clearly on a phone.
    """
    sections: list[tuple[str, object]] = [
        (t("products.lbl.product", lang), esc(product.title)),
        (t("products.lbl.category", lang), esc(category_title)),
    ]
    if product.type == "v2ray":
        sections.append((t("products.lbl.duration", lang),
                         t("products.duration_value", lang, days=product.duration_days or 0)))
        sections.append((t("products.lbl.traffic", lang), format_gb(product.traffic_gb)))
        if product.ip_limit:
            sections.append((t("products.lbl.ip_limit", lang), str(product.ip_limit)))
        if server_name:
            sections.append((t("products.lbl.server", lang), esc(server_name)))
    if product.description:
        sections.append((t("products.lbl.description", lang), esc(product.description)))
    sections.append((t("products.lbl.price", lang), format_money(product.price)))
    return render_big_message(t("products.invoice.title", lang), sections=sections).split("\n")


async def render_categories(
    message: Message, _: Callable[..., str], lang: str, groups=None
) -> None:
    """Step 1: category buttons. Falls back to a flat list when there is only one group."""
    async with SessionLocal() as session:
        if groups is None:
            groups = await product_category_service.grouped_for_bot(session)
    if not groups:
        await message.answer(_("products.user.empty"))
        return
    if len(groups) == 1:
        await render_category_products(message, _, lang, groups[0].category_id)
        return

    buttons: list[list[InlineKeyboardButton]] = []
    for group in groups:
        title = group.title or _("products.category.other")
        buttons.append([InlineKeyboardButton(
            text=title, callback_data=f"{CB_CAT}{_cat_payload(group.category_id)}")])
    await message.answer(
        _("products.category.choose"),
        reply_markup=InlineKeyboardMarkup(inline_keyboard=buttons),
    )


async def render_category_products(
    message: Message, _: Callable[..., str], lang: str, category_id: int | None
) -> None:
    """Step 2: products inside one group, each linking to its invoice."""
    async with SessionLocal() as session:
        groups = await product_category_service.grouped_for_bot(session)
    group = next((g for g in groups if g.category_id == category_id), None)
    multiple = len(groups) > 1
    title = (group.title if group and group.title else None) or _("products.category.other")

    if group is None or not group.products:
        rows: list[list[InlineKeyboardButton]] = []
        if multiple:
            rows.append([InlineKeyboardButton(
                text=_("btn.back_to_categories"), callback_data=CB_CATS)])
        await message.answer(
            _("products.category.empty"),
            reply_markup=InlineKeyboardMarkup(inline_keyboard=rows) if rows else None,
        )
        return

    lines = [_("products.category.products_header", title=title), ""]
    buttons = []
    for p in group.products:
        price = t("product.price_fmt", lang, price=f"{p.price:,}")
        lines.append(f"• <b>{p.title}</b> — {price}")
        buttons.append([InlineKeyboardButton(
            text=f"{p.title} — {price}", callback_data=f"{CB_DETAIL}{p.id}")])
    if multiple:
        buttons.append([InlineKeyboardButton(
            text=_("btn.back_to_categories"), callback_data=CB_CATS)])
    await message.answer(
        "\n".join(lines), parse_mode="HTML",
        reply_markup=InlineKeyboardMarkup(inline_keyboard=buttons),
    )


@router.message(Command("products"))
@router.message(F.text.in_(menu_texts("btn.products")))
async def on_products(
    message: Message, _: Callable[..., str], state: FSMContext, lang: str = "fa"
) -> None:
    await state.clear()  # leaving any pending receipt-wait
    await render_categories(message, _, lang)


@router.callback_query(F.data == CB_CATS)
@router.callback_query(F.data == CB_LIST)
async def on_categories_back(
    callback: CallbackQuery, _: Callable[..., str], lang: str = "fa"
) -> None:
    if callback.message is not None:
        await render_categories(callback.message, _, lang)
    await callback.answer()


@router.callback_query(F.data.startswith(CB_CAT))
async def on_category_open(
    callback: CallbackQuery, _: Callable[..., str], lang: str = "fa"
) -> None:
    try:
        category_id = _parse_cat_payload((callback.data or "")[len(CB_CAT):])
    except ValueError:
        await callback.answer()
        return
    if callback.message is not None:
        await render_category_products(callback.message, _, lang, category_id)
    await callback.answer()


@router.callback_query(F.data.startswith(CB_DETAIL))
async def on_product_detail(
    callback: CallbackQuery, _: Callable[..., str], lang: str = "fa"
) -> None:
    from app.bot.handlers.user import orders as orders_mod

    product_id = int((callback.data or "0")[len(CB_DETAIL):])
    async with SessionLocal() as session:
        product = await product_service.get(session, product_id)
        if product is None or not product.is_active or product.is_hidden:
            await callback.answer(_("products.unknown"), show_alert=True)
            return
        # A safe, user-facing server label only — never base_url/username/inbound.
        server_name: str | None = None
        if product.type == "v2ray" and product.xui_server_id:
            server = await xui_server_service.get_server(session, product.xui_server_id)
            server_name = server.name if server is not None else None
        # Effective category for the invoice line + the "back" target: an inactive
        # or missing category collapses to the synthetic Other group.
        category_title = _("products.category.other")
        back_id: int | None = None
        if product.category_id:
            category = await product_category_service.get(session, product.category_id)
            if category is not None and category.is_active:
                category_title = category.title
                back_id = category.id
        pay_rows, pay_note = await orders_mod.payment_method_rows(session, product.id, _)

    lines = build_invoice_lines(product, category_title, server_name, lang)
    keyboard_rows = list(pay_rows)
    if pay_note:
        lines += ["", pay_note]
    else:
        lines += ["", _("products.invoice.choose_payment")]
    keyboard_rows.append([InlineKeyboardButton(
        text=_("btn.back"), callback_data=f"{CB_CAT}{_cat_payload(back_id)}")])

    if callback.message is not None:
        await callback.message.answer(
            "\n".join(lines), parse_mode="HTML",
            reply_markup=InlineKeyboardMarkup(inline_keyboard=keyboard_rows),
        )
    await callback.answer()

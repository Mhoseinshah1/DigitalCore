"""User product list + detail. Buying is wired to the Phase 3 order flow
(see app/bot/handlers/user/orders.py, which owns the Buy callback)."""
from __future__ import annotations

from collections.abc import Callable

from aiogram import F, Router
from aiogram.filters import Command
from aiogram.fsm.context import FSMContext
from aiogram.types import CallbackQuery, InlineKeyboardButton, InlineKeyboardMarkup, Message

from app.database import SessionLocal
from app.i18n import t, texts_for
from app.models.product import Product
from app.services import product_service, xui_server_service

router = Router(name="user.products")

CB_DETAIL = "uprod:"
CB_BUY = "ubuy:"
CB_LIST = "uprods"


def _price_text(product: Product, lang: str) -> str:
    return t("product.price_fmt", lang, price=f"{product.price:,}")


def _summary_line(product: Product, lang: str) -> str:
    parts = [f"<b>{product.title}</b>", _price_text(product, lang)]
    if product.type == "v2ray":
        parts.append(t("product.duration_fmt", lang, days=product.duration_days))
        parts.append(t("product.traffic_fmt", lang, gb=product.traffic_gb))
    return " · ".join(parts)


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


async def render_product_list(message: Message, _: Callable[..., str], lang: str) -> None:
    async with SessionLocal() as session:
        products = await product_service.list_for_user(session)

    if not products:
        await message.answer(_("products.user.empty"))
        return

    lines = [_("products.user.header"), ""]
    buttons: list[list[InlineKeyboardButton]] = []
    for p in products:
        lines.append(f"• {_summary_line(p, lang)}")
        buttons.append(
            [InlineKeyboardButton(text=p.title, callback_data=f"{CB_DETAIL}{p.id}")]
        )
    await message.answer(
        "\n".join(lines),
        reply_markup=InlineKeyboardMarkup(inline_keyboard=buttons),
        parse_mode="HTML",
    )


@router.message(Command("products"))
@router.message(F.text.in_(texts_for("btn.products")))
async def on_products(
    message: Message, _: Callable[..., str], state: FSMContext, lang: str = "fa"
) -> None:
    await state.clear()  # leaving any pending receipt-wait
    await render_product_list(message, _, lang)


@router.callback_query(F.data == CB_LIST)
async def on_products_back(
    callback: CallbackQuery, _: Callable[..., str], lang: str = "fa"
) -> None:
    if isinstance(callback.message, Message):
        await render_product_list(callback.message, _, lang)
    await callback.answer()


@router.callback_query(F.data.startswith(CB_DETAIL))
async def on_product_detail(
    callback: CallbackQuery, _: Callable[..., str], lang: str = "fa"
) -> None:
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

    lines = build_detail_lines(product, server_name, lang)
    keyboard = InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text=_("btn.buy"), callback_data=f"{CB_BUY}{product.id}")],
        [InlineKeyboardButton(text=_("btn.back"), callback_data=CB_LIST)],
    ])
    if isinstance(callback.message, Message):
        await callback.message.answer(
            "\n".join(lines), parse_mode="HTML", reply_markup=keyboard
        )
    await callback.answer()

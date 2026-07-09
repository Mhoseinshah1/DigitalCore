"""Telegram admin product management (RBAC-gated FSM, bilingual).

Add flow: pick type -> title -> price -> (v2ray: duration -> traffic) -> create.
Manage: pick a product -> toggle active/hidden or edit title/price/duration/
traffic via a value prompt. All persistence/validation lives in
app/services/product_service (which audit-logs every change).
"""
from __future__ import annotations

import logging
from collections.abc import Callable

from aiogram import F, Router
from aiogram.fsm.context import FSMContext
from aiogram.filters import Command
from aiogram.types import CallbackQuery, InlineKeyboardButton, InlineKeyboardMarkup, Message

from app.bot.states.products import ProductAddForm, ProductEditForm
from app.bot.utils.message_format import esc, format_gb, format_money, render_big_message
from app.core.permissions import Role, has_permission
from app.database import SessionLocal
from app.i18n import t, texts_for
from app.models.product import PRODUCT_TYPES, Product
from app.models.xui_inbound import XuiInbound
from app.schemas.product import ProductCreate, ProductUpdate
from app.services import product_service, xui_server_service

log = logging.getLogger("bot.products")

router = Router(name="admin.products")

CB_LIST = "prod:list"
CB_ADD = "prod:add"
CB_TYPE = "ptype:"
CB_VIEW = "prod:view:"
CB_TOGGLE = "prod:toggle:"
CB_HIDE = "prod:hide:"
CB_EDIT = "prod:edit:"

# V2Ray add-flow callbacks (server + inbound binding, optional extras).
CB_ADD_SRV = "padd:srv:"       # padd:srv:<server_id>  — server chosen
CB_ADD_SRVLIST = "padd:srvs"   # re-show the server picker
CB_ADD_INB = "padd:inb:"       # padd:inb:<inbound_record_id> — inbound chosen
CB_ADD_SYNC = "padd:sync:"     # padd:sync:<server_id> — sync-inbounds guidance
CB_ADD_SKIP_IP = "padd:skipip"
CB_ADD_SKIP_DESC = "padd:skipdesc"
CB_ADD_CANCEL = "padd:cancel"

EDITABLE_FIELDS = ("title", "price", "duration_days", "traffic_gb")

FIELD_BTN_KEYS = {
    "title": "btn.prod.edit_title",
    "price": "btn.prod.edit_price",
    "duration_days": "btn.prod.edit_duration",
    "traffic_gb": "btn.prod.edit_traffic",
}
INT_FIELDS = {"price", "duration_days", "traffic_gb"}


def _state_text(product: Product, lang: str) -> str:
    if not product.is_active:
        return t("product.state.inactive", lang)
    if product.is_hidden:
        return t("product.state.hidden", lang)
    return t("product.state.active", lang)


def _product_line(product: Product, lang: str) -> str:
    price = t("product.price_fmt", lang, price=f"{product.price:,}")
    parts = [f"<b>{product.title}</b>", t(f"product.type.{product.type}", lang), price]
    if product.type == "v2ray":
        parts.append(t("product.duration_fmt", lang, days=product.duration_days))
        parts.append(t("product.traffic_fmt", lang, gb=product.traffic_gb))
    parts.append(_state_text(product, lang))
    return " · ".join(parts)


async def _admin_overview(lang: str) -> tuple[str, InlineKeyboardMarkup]:
    async with SessionLocal() as session:
        products = await product_service.list_for_admin(session)

    lines = [t("products.admin.header", lang), ""]
    buttons: list[list[InlineKeyboardButton]] = []
    if not products:
        lines.append(t("products.admin.empty", lang))
    for p in products:
        lines.append(f"• {_product_line(p, lang)}")
        buttons.append(
            [InlineKeyboardButton(text=p.title, callback_data=f"{CB_VIEW}{p.id}")]
        )
    buttons.append(
        [InlineKeyboardButton(text=t("products.admin.add", lang), callback_data=CB_ADD)]
    )
    return "\n".join(lines), InlineKeyboardMarkup(inline_keyboard=buttons)


def _detail_keyboard(product: Product, lang: str) -> InlineKeyboardMarkup:
    rows = [
        [
            InlineKeyboardButton(
                text=t("btn.prod.toggle_active", lang),
                callback_data=f"{CB_TOGGLE}{product.id}",
            ),
            InlineKeyboardButton(
                text=t("btn.prod.toggle_hidden", lang),
                callback_data=f"{CB_HIDE}{product.id}",
            ),
        ],
        [
            InlineKeyboardButton(
                text=t(FIELD_BTN_KEYS["title"], lang),
                callback_data=f"{CB_EDIT}{product.id}:title",
            ),
            InlineKeyboardButton(
                text=t(FIELD_BTN_KEYS["price"], lang),
                callback_data=f"{CB_EDIT}{product.id}:price",
            ),
        ],
    ]
    if product.type == "v2ray":
        rows.append(
            [
                InlineKeyboardButton(
                    text=t(FIELD_BTN_KEYS["duration_days"], lang),
                    callback_data=f"{CB_EDIT}{product.id}:duration_days",
                ),
                InlineKeyboardButton(
                    text=t(FIELD_BTN_KEYS["traffic_gb"], lang),
                    callback_data=f"{CB_EDIT}{product.id}:traffic_gb",
                ),
            ]
        )
    rows.append([InlineKeyboardButton(text=t("btn.prod.back", lang), callback_data=CB_LIST)])
    return InlineKeyboardMarkup(inline_keyboard=rows)


@router.message(F.text.in_(texts_for("btn.admin.products")))
async def on_products_menu(
    message: Message, _: Callable[..., str], lang: str = "fa", role: Role | None = None
) -> None:
    if not has_permission(role, "manage_products"):
        await message.answer(_("products.not_authorized"))
        return
    text, keyboard = await _admin_overview(lang)
    await message.answer(text, reply_markup=keyboard, parse_mode="HTML")


@router.callback_query(F.data == CB_LIST)
async def on_back_to_list(
    callback: CallbackQuery, _: Callable[..., str], lang: str = "fa", role: Role | None = None
) -> None:
    if not has_permission(role, "manage_products"):
        await callback.answer(_("products.not_authorized"), show_alert=True)
        return
    text, keyboard = await _admin_overview(lang)
    # isinstance (not `is not None`): an InaccessibleMessage has no edit_text.
    if isinstance(callback.message, Message):
        await callback.message.edit_text(text, reply_markup=keyboard, parse_mode="HTML")
    await callback.answer()


@router.callback_query(F.data == CB_ADD)
async def on_add(
    callback: CallbackQuery,
    state: FSMContext,
    _: Callable[..., str],
    lang: str = "fa",
    role: Role | None = None,
) -> None:
    if not has_permission(role, "manage_products"):
        await callback.answer(_("products.not_authorized"), show_alert=True)
        return
    keyboard = InlineKeyboardMarkup(
        inline_keyboard=[
            [
                InlineKeyboardButton(
                    text=t(f"product.type.{pt}", lang), callback_data=f"{CB_TYPE}{pt}"
                )
                for pt in PRODUCT_TYPES
            ]
        ]
    )
    if callback.message is not None:
        await callback.message.answer(_("products.pick_type"), reply_markup=keyboard)
    await callback.answer()


@router.callback_query(F.data.startswith(CB_TYPE))
async def on_type_chosen(
    callback: CallbackQuery,
    state: FSMContext,
    _: Callable[..., str],
    role: Role | None = None,
) -> None:
    if not has_permission(role, "manage_products"):
        await callback.answer(_("products.not_authorized"), show_alert=True)
        return
    product_type = (callback.data or "")[len(CB_TYPE):]
    if product_type not in PRODUCT_TYPES:
        await callback.answer(_("products.unknown"), show_alert=True)
        return
    await state.set_state(ProductAddForm.entering_title)
    # Remember who is adding, so a create finished from a callback (skip button)
    # still audits under the real admin, not the bot account.
    await state.update_data(type=product_type, admin_id=callback.from_user.id)
    if callback.message is not None:
        await callback.message.answer(_("products.ask_title"))
    await callback.answer()


@router.message(ProductAddForm.entering_title, Command("cancel"))
@router.message(ProductAddForm.entering_price, Command("cancel"))
@router.message(ProductAddForm.entering_duration, Command("cancel"))
@router.message(ProductAddForm.entering_traffic, Command("cancel"))
@router.message(ProductAddForm.entering_ip_limit, Command("cancel"))
@router.message(ProductAddForm.entering_description, Command("cancel"))
@router.message(ProductEditForm.entering_value, Command("cancel"))
async def on_cancel(message: Message, state: FSMContext, _: Callable[..., str]) -> None:
    await state.clear()
    await message.answer(_("products.cancelled"))


@router.message(ProductAddForm.entering_title, F.text)
async def on_title(message: Message, state: FSMContext, _: Callable[..., str]) -> None:
    title = (message.text or "").strip()
    if not title:
        await message.answer(_("products.ask_title"))
        return
    await state.update_data(title=title)
    await state.set_state(ProductAddForm.entering_price)
    await message.answer(_("products.ask_price"))


def _parse_positive_int(text: str) -> int | None:
    try:
        value = int(text.strip().replace(",", ""))
    except (TypeError, ValueError):
        return None
    return value if value >= 0 else None


@router.message(ProductAddForm.entering_price, F.text)
async def on_price(
    message: Message, state: FSMContext, _: Callable[..., str], role: Role | None = None
) -> None:
    price = _parse_positive_int(message.text or "")
    if price is None:
        await message.answer(_("products.invalid_number"))
        return
    await state.update_data(price=price)
    data = await state.get_data()
    if data.get("type") == "v2ray":
        await state.set_state(ProductAddForm.entering_duration)
        await message.answer(_("products.ask_duration"))
        return
    await _finish_create(message, state, _, role)


@router.message(ProductAddForm.entering_duration, F.text)
async def on_duration(message: Message, state: FSMContext, _: Callable[..., str]) -> None:
    duration = _parse_positive_int(message.text or "")
    if not duration:
        await message.answer(_("products.invalid_number"))
        return
    await state.update_data(duration_days=duration)
    await state.set_state(ProductAddForm.entering_traffic)
    await message.answer(_("products.ask_traffic"))


@router.message(ProductAddForm.entering_traffic, F.text)
async def on_traffic(
    message: Message, state: FSMContext, _: Callable[..., str],
    lang: str = "fa", role: Role | None = None,
) -> None:
    traffic = _parse_positive_int(message.text or "")
    if not traffic:
        await message.answer(_("products.invalid_number"))
        return
    await state.update_data(traffic_gb=traffic)
    # A V2Ray product MUST be bound to an XUI server + inbound — collect them now
    # (the old flow skipped this and product_service rejected the create).
    await _prompt_server(message, state, _, lang)


# --------------------------------------------------------------------------
# V2Ray binding: server picker -> inbound picker -> optional extras -> create.
# --------------------------------------------------------------------------
def _server_label(server) -> str:
    status = (server.status or "").strip()
    return f"{server.name} · {status}" if status and status != "unknown" else server.name


def _inbound_label(inbound: XuiInbound) -> str:
    name = inbound.remark or inbound.tag or f"inbound {inbound.inbound_id}"
    proto = (inbound.protocol or "").strip()
    if proto and inbound.port:
        return f"{name} · {proto}:{inbound.port}"
    return name


def _cancel_row(_: Callable[..., str]) -> list[InlineKeyboardButton]:
    return [InlineKeyboardButton(text=_("products.v2ray.cancel_btn"), callback_data=CB_ADD_CANCEL)]


async def _prompt_server(reply: Message, state: FSMContext, _: Callable[..., str], lang: str) -> None:
    """Show active XUI servers, or a clear error when none exist."""
    async with SessionLocal() as session:
        servers = await xui_server_service.list_servers(session, active_only=True)
    if not servers:
        await state.clear()
        await reply.answer(_("products.v2ray.no_server"), parse_mode="HTML")
        return
    rows = [[InlineKeyboardButton(text=_server_label(s), callback_data=f"{CB_ADD_SRV}{s.id}")]
            for s in servers]
    rows.append(_cancel_row(_))
    await state.set_state(ProductAddForm.choosing_server)
    await reply.answer(_("products.v2ray.pick_server"),
                       reply_markup=InlineKeyboardMarkup(inline_keyboard=rows))


async def _prompt_inbound(reply: Message, state: FSMContext, server_id: int,
                          _: Callable[..., str]) -> None:
    """Show active synced inbounds for a server, or a clear error + guidance."""
    async with SessionLocal() as session:
        server = await xui_server_service.get_server(session, server_id)
        if server is None:
            await state.clear()
            await reply.answer(_("products.unknown"))
            return
        inbounds = await xui_server_service.list_inbounds(session, server_id, active_only=True)
        server_name = server.name
    if not inbounds:
        rows = [
            [InlineKeyboardButton(text=_("products.v2ray.sync_btn"),
                                  callback_data=f"{CB_ADD_SYNC}{server_id}")],
            [InlineKeyboardButton(text=_("products.v2ray.other_server_btn"),
                                  callback_data=CB_ADD_SRVLIST)],
            _cancel_row(_),
        ]
        await reply.answer(_("products.v2ray.no_inbound"), parse_mode="HTML",
                           reply_markup=InlineKeyboardMarkup(inline_keyboard=rows))
        return
    rows = [[InlineKeyboardButton(text=_inbound_label(i), callback_data=f"{CB_ADD_INB}{i.id}")]
            for i in inbounds]
    rows.append([InlineKeyboardButton(text=_("products.v2ray.other_server_btn"),
                                      callback_data=CB_ADD_SRVLIST)])
    rows.append(_cancel_row(_))
    await state.set_state(ProductAddForm.choosing_inbound)
    await state.update_data(xui_server_id=server_id, server_name=server_name)
    await reply.answer(_("products.v2ray.pick_inbound", server=server_name),
                       reply_markup=InlineKeyboardMarkup(inline_keyboard=rows))


@router.callback_query(F.data == CB_ADD_SRVLIST)
async def on_add_server_list(
    callback: CallbackQuery, state: FSMContext, _: Callable[..., str],
    lang: str = "fa", role: Role | None = None,
) -> None:
    if not has_permission(role, "manage_products"):
        await callback.answer(_("products.not_authorized"), show_alert=True)
        return
    await callback.answer()
    if callback.message is not None:
        await _prompt_server(callback.message, state, _, lang)


@router.callback_query(F.data.startswith(CB_ADD_SRV))
async def on_add_server_chosen(
    callback: CallbackQuery, state: FSMContext, _: Callable[..., str],
    lang: str = "fa", role: Role | None = None,
) -> None:
    if not has_permission(role, "manage_products"):
        await callback.answer(_("products.not_authorized"), show_alert=True)
        return
    raw = (callback.data or "")[len(CB_ADD_SRV):]
    if not raw.isdigit():
        await callback.answer(_("products.unknown"), show_alert=True)
        return
    await callback.answer()
    if callback.message is not None:
        await _prompt_inbound(callback.message, state, int(raw), _)


@router.callback_query(F.data.startswith(CB_ADD_SYNC))
async def on_add_sync_hint(
    callback: CallbackQuery, _: Callable[..., str], role: Role | None = None
) -> None:
    # Syncing from inside the bot is intentionally not offered (risky); guide the
    # admin to the web panel instead.
    if not has_permission(role, "manage_products"):
        await callback.answer(_("products.not_authorized"), show_alert=True)
        return
    await callback.answer()
    if callback.message is not None:
        await callback.message.answer(_("products.v2ray.sync_hint"), parse_mode="HTML")


@router.callback_query(F.data.startswith(CB_ADD_INB))
async def on_add_inbound_chosen(
    callback: CallbackQuery, state: FSMContext, _: Callable[..., str],
    lang: str = "fa", role: Role | None = None,
) -> None:
    if not has_permission(role, "manage_products"):
        await callback.answer(_("products.not_authorized"), show_alert=True)
        return
    raw = (callback.data or "")[len(CB_ADD_INB):]
    if not raw.isdigit():
        await callback.answer(_("products.unknown"), show_alert=True)
        return
    async with SessionLocal() as session:
        inbound = await xui_server_service.get_inbound(session, int(raw))
    if inbound is None:
        await callback.answer(_("products.unknown"), show_alert=True)
        return
    await state.update_data(xui_inbound_id=inbound.id, inbound_remark=_inbound_label(inbound))
    await state.set_state(ProductAddForm.entering_ip_limit)
    await callback.answer()
    if callback.message is not None:
        kb = InlineKeyboardMarkup(inline_keyboard=[
            [InlineKeyboardButton(text=_("products.v2ray.skip_btn"), callback_data=CB_ADD_SKIP_IP)]])
        await callback.message.answer(_("products.v2ray.ask_ip_limit"), reply_markup=kb)


@router.message(ProductAddForm.entering_ip_limit, F.text)
async def on_ip_limit(
    message: Message, state: FSMContext, _: Callable[..., str], role: Role | None = None
) -> None:
    ip = _parse_positive_int(message.text or "")
    if ip is None:
        await message.answer(_("products.invalid_number"))
        return
    await state.update_data(ip_limit=ip or None)  # 0 = unlimited
    await _prompt_description(message, state, _)


@router.callback_query(F.data == CB_ADD_SKIP_IP)
async def on_skip_ip(
    callback: CallbackQuery, state: FSMContext, _: Callable[..., str], role: Role | None = None
) -> None:
    await state.update_data(ip_limit=None)
    await callback.answer()
    if callback.message is not None:
        await _prompt_description(callback.message, state, _)


async def _prompt_description(reply: Message, state: FSMContext, _: Callable[..., str]) -> None:
    await state.set_state(ProductAddForm.entering_description)
    kb = InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text=_("products.v2ray.skip_btn"), callback_data=CB_ADD_SKIP_DESC)]])
    await reply.answer(_("products.v2ray.ask_description"), reply_markup=kb)


@router.message(ProductAddForm.entering_description, F.text)
async def on_description(
    message: Message, state: FSMContext, _: Callable[..., str], role: Role | None = None
) -> None:
    desc = (message.text or "").strip()
    await state.update_data(description=desc or None)
    await _finish_create(message, state, _, role)


@router.callback_query(F.data == CB_ADD_SKIP_DESC)
async def on_skip_desc(
    callback: CallbackQuery, state: FSMContext, _: Callable[..., str], role: Role | None = None
) -> None:
    await state.update_data(description=None)
    await callback.answer()
    if callback.message is not None:
        await _finish_create(callback.message, state, _, role)


@router.callback_query(F.data == CB_ADD_CANCEL)
async def on_add_cancel(
    callback: CallbackQuery, state: FSMContext, _: Callable[..., str], role: Role | None = None
) -> None:
    await state.clear()
    await callback.answer()
    if callback.message is not None:
        await callback.message.answer(_("products.cancelled"))


def _v2ray_created_message(
    product: Product, data: dict, _: Callable[..., str]
) -> str:
    sections: list[tuple[str, object]] = [
        (_("products.lbl.title"), esc(product.title)),
        (_("products.lbl.type"), _("product.type.v2ray")),
        (_("products.lbl.price"), format_money(product.price)),
        (_("products.lbl.duration"), _("products.duration_value", days=product.duration_days or 0)),
        (_("products.lbl.traffic"), format_gb(product.traffic_gb)),
        (_("products.lbl.server"), esc(data.get("server_name") or "—")),
        (_("products.lbl.inbound"), esc(data.get("inbound_remark") or "—")),
    ]
    if product.ip_limit:
        sections.append((_("products.lbl.ip_limit"), str(product.ip_limit)))
    if product.description:
        sections.append((_("products.lbl.description"), esc(product.description)))
    return render_big_message(_("products.created_title"), sections=sections,
                              footer=_("products.created_footer"))


async def _finish_create(
    message: Message, state: FSMContext, _: Callable[..., str], role: Role | None
) -> None:
    if not has_permission(role, "manage_products"):
        await state.clear()
        await message.answer(_("products.not_authorized"))
        return
    data = await state.get_data()
    await state.clear()
    actor_id = data.get("admin_id") or (message.from_user.id if message.from_user else None)
    try:
        async with SessionLocal() as session:
            product = await product_service.create(
                session,
                ProductCreate(
                    type=str(data.get("type", "")),
                    title=str(data.get("title", "")),
                    price=int(data.get("price", 0)),
                    duration_days=data.get("duration_days"),
                    traffic_gb=data.get("traffic_gb"),
                    ip_limit=data.get("ip_limit"),
                    xui_server_id=data.get("xui_server_id"),
                    xui_inbound_id=data.get("xui_inbound_id"),
                    description=data.get("description"),
                ),
                actor_type="admin",
                actor_id=actor_id,
            )
            await session.commit()
    except ValueError as exc:
        await message.answer(_("products.invalid", error=exc))
        return
    if product.type == "v2ray":
        await message.answer(_v2ray_created_message(product, data, _), parse_mode="HTML")
    else:
        await message.answer(_("products.created", title=product.title))


@router.callback_query(F.data.startswith(CB_VIEW))
async def on_view(
    callback: CallbackQuery, _: Callable[..., str], lang: str = "fa", role: Role | None = None
) -> None:
    if not has_permission(role, "manage_products"):
        await callback.answer(_("products.not_authorized"), show_alert=True)
        return
    product_id = int((callback.data or "0")[len(CB_VIEW):])
    async with SessionLocal() as session:
        product = await product_service.get(session, product_id)
    if product is None:
        await callback.answer(_("products.unknown"), show_alert=True)
        return
    text = _product_line(product, lang)
    if product.description:
        text += f"\n{product.description}"
    if isinstance(callback.message, Message):  # InaccessibleMessage has no edit_text
        await callback.message.edit_text(
            text, reply_markup=_detail_keyboard(product, lang), parse_mode="HTML"
        )
    await callback.answer()


async def _apply_toggle(
    callback: CallbackQuery, lang: str, *, hidden_toggle: bool
) -> None:
    prefix = CB_HIDE if hidden_toggle else CB_TOGGLE
    product_id = int((callback.data or "0")[len(prefix):])
    tg_user = callback.from_user
    async with SessionLocal() as session:
        product = await product_service.get(session, product_id)
        if product is None:
            await callback.answer(t("products.unknown", lang), show_alert=True)
            return
        if hidden_toggle:
            product = await product_service.set_hidden(
                session, product_id, not product.is_hidden,
                actor_type="admin", actor_id=tg_user.id,
            )
        else:
            product = await product_service.set_active(
                session, product_id, not product.is_active,
                actor_type="admin", actor_id=tg_user.id,
            )
        await session.commit()
    await callback.answer(t("products.updated", lang))
    if isinstance(callback.message, Message) and product is not None:
        await callback.message.edit_text(
            _product_line(product, lang),
            reply_markup=_detail_keyboard(product, lang),
            parse_mode="HTML",
        )


@router.callback_query(F.data.startswith(CB_TOGGLE))
async def on_toggle_active(
    callback: CallbackQuery, _: Callable[..., str], lang: str = "fa", role: Role | None = None
) -> None:
    if not has_permission(role, "manage_products"):
        await callback.answer(_("products.not_authorized"), show_alert=True)
        return
    await _apply_toggle(callback, lang, hidden_toggle=False)


@router.callback_query(F.data.startswith(CB_HIDE))
async def on_toggle_hidden(
    callback: CallbackQuery, _: Callable[..., str], lang: str = "fa", role: Role | None = None
) -> None:
    if not has_permission(role, "manage_products"):
        await callback.answer(_("products.not_authorized"), show_alert=True)
        return
    await _apply_toggle(callback, lang, hidden_toggle=True)


@router.callback_query(F.data.startswith(CB_EDIT))
async def on_edit_field(
    callback: CallbackQuery,
    state: FSMContext,
    _: Callable[..., str],
    lang: str = "fa",
    role: Role | None = None,
) -> None:
    if not has_permission(role, "manage_products"):
        await callback.answer(_("products.not_authorized"), show_alert=True)
        return
    payload = (callback.data or "")[len(CB_EDIT):]
    product_id_text, _sep, field = payload.partition(":")
    if field not in EDITABLE_FIELDS or not product_id_text.isdigit():
        await callback.answer(_("products.unknown"), show_alert=True)
        return
    await state.set_state(ProductEditForm.entering_value)
    await state.update_data(product_id=int(product_id_text), field=field)
    field_label = t(FIELD_BTN_KEYS[field], lang)
    if callback.message is not None:
        await callback.message.answer(_("products.edit_prompt", field=field_label))
    await callback.answer()


@router.message(ProductEditForm.entering_value, F.text)
async def on_edit_value(
    message: Message,
    state: FSMContext,
    _: Callable[..., str],
    lang: str = "fa",
    role: Role | None = None,
) -> None:
    if not has_permission(role, "manage_products"):
        await state.clear()
        await message.answer(_("products.not_authorized"))
        return
    data = await state.get_data()
    product_id = int(data.get("product_id", 0))
    field = str(data.get("field", ""))
    raw = (message.text or "").strip()

    value: object = raw
    if field in INT_FIELDS:
        parsed = _parse_positive_int(raw)
        if parsed is None:
            await message.answer(_("products.invalid_number"))
            return
        value = parsed

    tg_user = message.from_user
    try:
        async with SessionLocal() as session:
            product = await product_service.update(
                session,
                product_id,
                ProductUpdate(**{field: value}),
                actor_type="admin",
                actor_id=tg_user.id if tg_user else None,
            )
            await session.commit()
    except ValueError as exc:
        await message.answer(_("products.invalid", error=exc))
        return

    await state.clear()
    if product is None:
        await message.answer(_("products.unknown"))
        return
    await message.answer(_("products.updated"))

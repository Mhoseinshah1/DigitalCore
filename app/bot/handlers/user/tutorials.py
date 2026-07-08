"""Phase 9 user tutorials: /tutorials — browse categories / platforms and read a
guide. Only ACTIVE tutorials/categories are shown. Content is sent as plain text
(no parse_mode) so admin-authored content can never inject Telegram HTML.

`uconn:<product_type>` is the "connection guide" entry point shown as a button
after a V2Ray delivery.
"""
from __future__ import annotations

import logging
from collections.abc import Callable

from aiogram import F, Router
from aiogram.filters import Command
from aiogram.fsm.context import FSMContext
from aiogram.types import (
    CallbackQuery,
    InlineKeyboardButton,
    InlineKeyboardMarkup,
    Message,
)

from app.database import SessionLocal
from app.i18n import menu_texts
from app.services import tutorial_service

log = logging.getLogger("bot.user.tutorials")

router = Router(name="user.tutorials")

CB_CAT = "tut_c:"      # open a category
CB_PLATFORM = "tut_p:"  # filter by platform
CB_READ = "tut_r:"     # read a tutorial
CB_CONN = "uconn:"     # connection guide (by product_type), used after delivery

_PLATFORMS = ("android", "ios", "windows", "mac", "linux", "general")


def _read_kb(_: Callable[..., str], tutorials) -> InlineKeyboardMarkup:
    rows = [[InlineKeyboardButton(text=t.title[:40], callback_data=f"{CB_READ}{t.id}")]
            for t in tutorials]
    return InlineKeyboardMarkup(inline_keyboard=rows)


async def _show_home(message: Message, _: Callable[..., str]) -> None:
    async with SessionLocal() as session:
        if not await tutorial_service.tutorials_enabled(session):
            await message.answer(_("tutorials.disabled"))
            return
        categories = await tutorial_service.list_categories(session, active_only=True)
    rows: list[list[InlineKeyboardButton]] = []
    for c in categories:
        rows.append([InlineKeyboardButton(text=c.title[:40], callback_data=f"{CB_CAT}{c.id}")])
    # A platform quick-pick row (also matches "general" guides).
    rows.append([InlineKeyboardButton(text=_("tutorial.platform." + p),
                                      callback_data=f"{CB_PLATFORM}{p}") for p in _PLATFORMS[:3]])
    rows.append([InlineKeyboardButton(text=_("tutorial.platform." + p),
                                      callback_data=f"{CB_PLATFORM}{p}") for p in _PLATFORMS[3:]])
    await message.answer(_("tutorials.home"),
                         reply_markup=InlineKeyboardMarkup(inline_keyboard=rows))


@router.message(Command("tutorials"))
@router.message(F.text.in_(menu_texts("btn.tutorials")))
async def on_tutorials(
    message: Message, _: Callable[..., str], state: FSMContext, lang: str = "fa"
) -> None:
    await state.clear()
    await _show_home(message, _)


@router.callback_query(F.data.startswith(CB_CAT))
async def on_category(
    callback: CallbackQuery, _: Callable[..., str], lang: str = "fa"
) -> None:
    category_id = int((callback.data or "0")[len(CB_CAT):])
    async with SessionLocal() as session:
        if not await tutorial_service.tutorials_enabled(session):
            await callback.answer(_("tutorials.disabled"), show_alert=True)
            return
        tutorials = await tutorial_service.list_tutorials(
            session, active_only=True, category_id=category_id)
    await callback.answer()
    if callback.message is None:
        return
    if not tutorials:
        await callback.message.answer(_("tutorials.empty"))
        return
    await callback.message.answer(_("tutorials.pick"), reply_markup=_read_kb(_, tutorials))


@router.callback_query(F.data.startswith(CB_PLATFORM))
async def on_platform(
    callback: CallbackQuery, _: Callable[..., str], lang: str = "fa"
) -> None:
    platform = (callback.data or "")[len(CB_PLATFORM):]
    async with SessionLocal() as session:
        if not await tutorial_service.tutorials_enabled(session):
            await callback.answer(_("tutorials.disabled"), show_alert=True)
            return
        tutorials = await tutorial_service.list_tutorials(
            session, active_only=True, platform=platform)
    await callback.answer()
    if callback.message is None:
        return
    if not tutorials:
        await callback.message.answer(_("tutorials.empty"))
        return
    await callback.message.answer(_("tutorials.pick"), reply_markup=_read_kb(_, tutorials))


@router.callback_query(F.data.startswith(CB_CONN))
async def on_connection_guide(
    callback: CallbackQuery, _: Callable[..., str], lang: str = "fa"
) -> None:
    product_type = (callback.data or "")[len(CB_CONN):] or "v2ray"
    async with SessionLocal() as session:
        if not await tutorial_service.tutorials_enabled(session):
            await callback.answer(_("tutorials.disabled"), show_alert=True)
            return
        tutorials = await tutorial_service.list_tutorials(
            session, active_only=True, product_type=product_type)
    await callback.answer()
    if callback.message is None:
        return
    if not tutorials:
        await callback.message.answer(_("tutorials.empty"))
        return
    await callback.message.answer(_("tutorials.pick"), reply_markup=_read_kb(_, tutorials))


@router.callback_query(F.data.startswith(CB_READ))
async def on_read(
    callback: CallbackQuery, _: Callable[..., str], lang: str = "fa"
) -> None:
    tutorial_id = int((callback.data or "0")[len(CB_READ):])
    async with SessionLocal() as session:
        tut = await tutorial_service.get_tutorial(session, tutorial_id)
        # Never expose an inactive (draft/hidden) tutorial to users.
        if tut is None or not tut.is_active:
            await callback.answer(_("tutorials.not_found"), show_alert=True)
            return
        title, content = tut.title, tut.content
    await callback.answer()
    if callback.message is not None:
        # Plain text (no parse_mode) — content is admin free-text, never trusted HTML.
        await callback.message.answer(f"📚 {title}\n\n{content}")

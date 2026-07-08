"""Admin main menu entry (command + reply button), RBAC-gated."""
from __future__ import annotations

from collections.abc import Callable

from aiogram import F, Router
from aiogram.filters import Command
from aiogram.types import Message

from app.bot.handlers.admin.panel import build_overview
from app.bot.keyboards.admin import admin_main_menu
from app.bot.keyboards.user import user_main_menu_async
from app.core.permissions import Role, has_permission
from app.i18n import texts_for

router = Router(name="admin.menu")


@router.message(Command("admin"))
@router.message(F.text.in_(texts_for("btn.admin_panel")))
async def on_admin_menu(
    message: Message, _: Callable[..., str], lang: str = "fa", role: Role | None = None
) -> None:
    if role is None or not has_permission(role, "view_dashboard"):
        await message.answer(_("admin.not_authorized"))
        return
    header = _("admin.panel_title", role=role.value)
    overview = await build_overview(lang, _)
    await message.answer(
        f"{header}\n\n{overview}", reply_markup=admin_main_menu(lang), parse_mode="HTML"
    )


@router.message(F.text.in_(texts_for("btn.admin.back")))
async def on_back_to_user_menu(
    message: Message, _: Callable[..., str], lang: str = "fa", role: Role | None = None
) -> None:
    await message.answer(
        _("main_menu"), reply_markup=user_main_menu(lang, is_admin=role is not None)
    )

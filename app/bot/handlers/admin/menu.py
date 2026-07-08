"""Admin main menu entry (command + reply button), RBAC-gated."""
from __future__ import annotations

from collections.abc import Callable

from aiogram import F, Router
from aiogram.filters import Command
from aiogram.fsm.context import FSMContext
from aiogram.types import Message

from app.bot.handlers.admin.panel import build_overview
from app.bot.keyboards.admin import admin_main_menu
from app.bot.keyboards.user import user_main_menu_async
from app.core.permissions import Role, has_permission
from app.database import SessionLocal
from app.i18n import menu_texts, texts_for
from app.services.diagnostics import bot_state_report, format_report

router = Router(name="admin.menu")

# «منوی کاربر» / «بازگشت به منوی کاربر» / «User menu» — emoji, no-emoji and old
# cached variants all return the normal user menu (available to everyone).
USER_MENU_TEXTS = (
    menu_texts("btn.admin.back")
    | {"منوی کاربر", "بازگشت به منوی کاربر", "User menu", "Back to user menu"}
)


@router.message(Command("debug_bot_state"))
async def on_debug_bot_state(
    message: Message, _: Callable[..., str], lang: str = "fa", role: Role | None = None
) -> None:
    """Admin-only runtime diagnostic: migration/category/product/settings state."""
    if role is None:
        await message.answer(_("admin.not_authorized"))
        return
    async with SessionLocal() as session:
        report = await bot_state_report(session)
    await message.answer(f"<pre>{format_report(report)}</pre>", parse_mode="HTML")


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


@router.message(F.text.in_(USER_MENU_TEXTS))
async def on_back_to_user_menu(
    message: Message, _: Callable[..., str], state: FSMContext,
    lang: str = "fa", role: Role | None = None,
) -> None:
    """Return to the normal user menu. Works for everyone (not admin-gated) and
    clears any pending FSM flow so the user is never stuck mid-conversation."""
    await state.clear()
    await message.answer(
        _("main_menu"),
        reply_markup=await user_main_menu_async(lang, is_admin=role is not None),
    )

"""Admin main menu entry (command + reply button), RBAC-gated."""
from __future__ import annotations

from aiogram import F, Router
from aiogram.filters import Command
from aiogram.types import Message

from app.core.permissions import Role, has_permission
from app.bot.keyboards.admin import admin_main_menu
from app.bot.keyboards.user import BTN_ADMIN_PANEL, user_main_menu
from app.bot.keyboards.admin import BTN_BACK_TO_USER

router = Router(name="admin.menu")

NOT_AUTHORIZED = "⛔️ You are not authorized to use the admin panel."


@router.message(Command("admin"))
@router.message(F.text == BTN_ADMIN_PANEL)
async def on_admin_menu(message: Message, role: Role | None = None) -> None:
    if not has_permission(role, "view_dashboard") or role is None:
        await message.answer(NOT_AUTHORIZED)
        return
    await message.answer(
        f"🛠 Admin panel — role: {role.value}", reply_markup=admin_main_menu()
    )


@router.message(F.text == BTN_BACK_TO_USER)
async def on_back_to_user_menu(message: Message, role: Role | None = None) -> None:
    await message.answer("Main menu", reply_markup=user_main_menu(is_admin=role is not None))

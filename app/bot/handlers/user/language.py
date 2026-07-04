"""Language picker: shown to new users on /start, and any time via /language."""
from __future__ import annotations

from collections.abc import Callable

from aiogram import F, Router
from aiogram.filters import Command
from aiogram.types import CallbackQuery, InlineKeyboardButton, InlineKeyboardMarkup, Message

from app.bot.keyboards.user import user_main_menu
from app.database import SessionLocal
from app.i18n import SUPPORTED, normalize_lang, t, texts_for
from app.services import user_service

router = Router(name="user.language")

CB_PREFIX = "lang:"


def language_picker_keyboard() -> InlineKeyboardMarkup:
    # Labels are language-independent by design (each language in its own script).
    return InlineKeyboardMarkup(
        inline_keyboard=[
            [
                InlineKeyboardButton(text=t("lang.fa_label", "fa"), callback_data=f"{CB_PREFIX}fa"),
                InlineKeyboardButton(text=t("lang.en_label", "en"), callback_data=f"{CB_PREFIX}en"),
            ]
        ]
    )


@router.message(Command("language"))
@router.message(F.text.in_(texts_for("btn.language")))
async def on_language_command(
    message: Message, _: Callable[..., str], lang: str = "fa"
) -> None:
    await message.answer(_("lang.pick"), reply_markup=language_picker_keyboard())


@router.callback_query(F.data.startswith(CB_PREFIX))
async def on_language_chosen(callback: CallbackQuery, is_admin: bool = False) -> None:
    chosen = normalize_lang((callback.data or "")[len(CB_PREFIX):])
    if chosen not in SUPPORTED or callback.from_user is None:
        await callback.answer()
        return

    async with SessionLocal() as session:
        await user_service.set_language(session, callback.from_user.id, chosen)

    # Confirm and show the main menu in the NEWLY chosen language.
    await callback.answer(t("lang.saved", chosen))
    if isinstance(callback.message, Message):
        await callback.message.answer(
            t("lang.saved", chosen),
            reply_markup=user_main_menu(chosen, is_admin=is_admin),
        )

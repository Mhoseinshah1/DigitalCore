"""Admin dead-button safety net (registered LAST).

Any text an admin sends that no earlier router handled — an unknown command, a
stale/renamed menu button, a typo — gets a clear "not available / invalid"
reply instead of silence. Gated three ways so it never shadows a real flow:

  * ``StateFilter(None)`` — never fires mid-FSM (product add, settings edit,
    receipt wait, coupon entry, …), so multi-step conversations are untouched;
  * ``role is not None`` — only admins reach it (ordinary users are unaffected);
  * it is the very last router, so it only runs when nothing else matched.
"""
from __future__ import annotations

from collections.abc import Callable

from aiogram import F, Router
from aiogram.filters import StateFilter
from aiogram.types import Message

from app.core.permissions import Role

router = Router(name="admin.fallback")


@router.message(StateFilter(None), F.text)
async def on_unknown_admin_text(
    message: Message, _: Callable[..., str], role: Role | None = None
) -> None:
    if role is None:  # not an admin → let it fall through (no reply here)
        return
    await message.answer(_("admin.unknown_command"))

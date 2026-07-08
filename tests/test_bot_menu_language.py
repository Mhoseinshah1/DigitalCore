"""Menu/language robustness: language out of the menu, cached-button compat,
bot_default_language, and the «منوی کاربر» return handler."""
from __future__ import annotations

from typing import Any

import pytest_asyncio
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker, create_async_engine
from sqlalchemy.pool import StaticPool

import app.bot.handlers.admin.menu as admin_menu
import app.bot.handlers.user.account as account_mod
import app.bot.handlers.user.language as language_mod
import app.bot.handlers.user.start as start_mod
import app.bot.keyboards.user as kb_mod
import app.bot.notifications as notify_mod
from app.bot.keyboards.user import user_main_menu
from app.i18n import menu_texts, t
from app.models import Base, Setting, User
from app.services import user_service

FA = lambda key, **p: t(key, "fa", **p)  # noqa: E731


def _menu_labels(kb) -> list[str]:
    return [b.text for row in kb.keyboard for b in row]


def test_language_and_rules_not_in_main_menu() -> None:
    labels = _menu_labels(user_main_menu("fa"))
    joined = " ".join(labels)
    assert t("btn.language", "fa") not in labels
    assert "زبان" not in joined and "Language" not in joined
    assert t("btn.rules", "fa") not in labels
    assert "قوانین" not in joined
    # The core buttons are still there.
    assert t("btn.products", "fa") in labels
    assert t("btn.account", "fa") in labels


def test_user_menu_texts_cover_variants() -> None:
    s = admin_menu.USER_MENU_TEXTS
    assert "منوی کاربر" in s
    assert "بازگشت به منوی کاربر" in s
    assert t("btn.admin.back", "fa") in s
    assert "User menu" in s


class FU:
    def __init__(self, uid, first_name="U"):
        self.id = uid
        self.username = "u"
        self.first_name = first_name
        self.last_name = None
        self.language_code = "fa"


class FM:
    def __init__(self, from_user=None, text=""):
        self.from_user = from_user
        self.text = text
        self.answers: list[str] = []
        self.markups: list[Any] = []

    async def answer(self, text: str, **kw: Any) -> None:
        self.answers.append(text)
        self.markups.append(kw.get("reply_markup"))


class FState:
    def __init__(self):
        self.state = "some_state"
        self.cleared = False

    async def clear(self):
        self.state = None
        self.cleared = True

    async def set_state(self, s):
        self.state = s

    async def update_data(self, **kw):
        pass

    async def get_data(self):
        return {}


async def test_cached_language_button_shows_info(monkeypatch) -> None:
    msg = FM(FU(5001))
    await language_mod.on_language_button(msg, FA, lang="fa")
    assert msg.answers == [FA("lang.managed_by_admin")]


@pytest_asyncio.fixture
async def bound_db(monkeypatch):
    engine = create_async_engine("sqlite+aiosqlite://", poolclass=StaticPool,
                                 connect_args={"check_same_thread": False})
    async with engine.begin() as conn:
        await conn.run_sync(Base.metadata.create_all)
    maker = async_sessionmaker(engine, class_=AsyncSession, expire_on_commit=False)
    for mod in (start_mod, account_mod, kb_mod, notify_mod, admin_menu):
        monkeypatch.setattr(mod, "SessionLocal", maker)
    try:
        yield maker
    finally:
        await engine.dispose()


async def test_start_new_user_adopts_default_language(bound_db) -> None:
    async with bound_db() as s:
        s.add(Setting(key="bot_default_language", value="en"))
        await s.commit()
    # New user; middleware would resolve lang=en, but we pass fa to prove /start
    # persists the admin default onto the user.
    msg = FM(FU(5002, "New"))
    await start_mod.on_start(msg, FA, lang="fa", is_admin=False)
    assert FA("lang.pick") not in msg.answers            # no picker
    async with bound_db() as s:
        user = await user_service.get_by_telegram_id(s, 5002)
    assert user is not None and user.language == "en"    # adopted admin default


async def test_user_menu_returns_menu_and_clears_state(bound_db) -> None:
    async with bound_db() as s:
        s.add(User(telegram_id=5003, first_name="U", language="fa"))
        await s.commit()
    state = FState()
    msg = FM(FU(5003), text="منوی کاربر")
    # role=None → a normal (non-admin) user still gets the user menu.
    await admin_menu.on_back_to_user_menu(msg, FA, state, lang="fa", role=None)
    assert state.cleared is True
    assert msg.markups[0] is not None
    labels = _menu_labels(msg.markups[0])
    assert t("btn.products", "fa") in labels

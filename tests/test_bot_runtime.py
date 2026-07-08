"""Production/runtime regressions for the bot UX phase:

- tolerant reply-keyboard matching (emoji + no-emoji / cached labels),
- the runtime diagnostic (survives a stale schema; flags migration 0019),
- /start still refreshes the keyboard without the قوانین button.
"""
from __future__ import annotations

from typing import Any

import pytest_asyncio
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker, create_async_engine
from sqlalchemy.pool import StaticPool

import app.bot.handlers.user.account as account_mod
import app.bot.handlers.user.products as products_mod
import app.bot.handlers.user.start as start_mod
import app.bot.keyboards.user as kb_mod
import app.bot.notifications as notify_mod
from app.i18n import menu_texts, strip_menu_decoration, t
from app.models import Base, Product, Setting, User
from app.services import product_category_service, product_service
from app.services.diagnostics import bot_state_report, format_report

FA = lambda key, **p: t(key, "fa", **p)  # noqa: E731


# --------------------------------------------------------------------------
# Tolerant menu matching (cached keyboards / typed labels)
# --------------------------------------------------------------------------
def test_menu_texts_include_emoji_and_stripped_variants() -> None:
    for key, stripped in [
        ("btn.products", "محصولات"),
        ("btn.account", "حساب من"),
        ("btn.wallet", "کیف پول"),
        ("btn.my_orders", "سفارش‌های من"),
        ("btn.rules", "قوانین"),
    ]:
        variants = menu_texts(key)
        assert t(key, "fa") in variants          # exact production label (with emoji)
        assert stripped in variants              # typed / de-emojified variant


def test_strip_menu_decoration_handles_all_emojis() -> None:
    assert strip_menu_decoration("🛍 محصولات") == "محصولات"
    assert strip_menu_decoration("👤 حساب من") == "حساب من"
    assert strip_menu_decoration("ℹ️ قوانین") == "قوانین"
    assert strip_menu_decoration("محصولات") == "محصولات"  # already plain


def test_products_and_account_handlers_use_tolerant_matching() -> None:
    # The reply-button handlers match on menu_texts(...), so both the exact
    # emoji label and the typed variant route correctly.
    prod = menu_texts("btn.products")
    acc = menu_texts("btn.account")
    assert {"🛍 محصولات", "محصولات"} <= prod
    assert {"👤 حساب من", "حساب من"} <= acc


# --------------------------------------------------------------------------
# Runtime diagnostic
# --------------------------------------------------------------------------
@pytest_asyncio.fixture
async def db():
    engine = create_async_engine("sqlite+aiosqlite://", poolclass=StaticPool,
                                 connect_args={"check_same_thread": False})
    async with engine.begin() as conn:
        await conn.run_sync(Base.metadata.create_all)
    maker = async_sessionmaker(engine, class_=AsyncSession, expire_on_commit=False)
    try:
        yield maker
    finally:
        await engine.dispose()


async def test_diagnostic_healthy_schema(db) -> None:
    async with db() as s:
        c = await product_category_service.create(s, title="اپل آیدی")
        await s.commit()
        await product_service.create(s, __import__("app.schemas.product", fromlist=["ProductCreate"])
                                     .ProductCreate(type="license", title="ID", price=1000, category_id=c.id))
        await s.commit()
        report = await bot_state_report(s)
    assert report["migration_0019_applied"] is True
    assert report["category_count"] == 1
    assert report["product_count"] == 1
    assert "wallet_payment_enabled" in report["settings"]


def test_diagnostic_format_flags_stale_migration() -> None:
    stale = {
        "migration_0019_applied": False,
        "schema": {"product_categories_table": False, "products_category_id_column": False},
        "product_count": 5, "settings": {},
    }
    txt = format_report(stale)
    assert "alembic upgrade head" in txt
    assert "stale" in txt.lower()


def test_diagnostic_format_notes_no_categories() -> None:
    healthy_empty = {
        "migration_0019_applied": True,
        "schema": {"product_categories_table": True, "products_category_id_column": True},
        "category_count": 0, "active_category_count": 0,
        "product_count": 3, "browsable_product_count": 3, "settings": {},
    }
    txt = format_report(healthy_empty)
    assert "no categories exist" in txt.lower()


# --------------------------------------------------------------------------
# /start refresh + tolerant account routing (end-to-end-ish)
# --------------------------------------------------------------------------
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

    async def answer(self, text: str, **kwargs: Any) -> None:
        self.answers.append(text)
        self.markups.append(kwargs.get("reply_markup"))


class FState:
    def __init__(self):
        self._data: dict = {}
        self.state = None

    async def clear(self):
        self._data = {}
        self.state = None

    async def set_state(self, state):
        self.state = state

    async def update_data(self, **kw):
        self._data.update(kw)

    async def get_data(self):
        return dict(self._data)


@pytest_asyncio.fixture
async def bound_db(monkeypatch):
    engine = create_async_engine("sqlite+aiosqlite://", poolclass=StaticPool,
                                 connect_args={"check_same_thread": False})
    async with engine.begin() as conn:
        await conn.run_sync(Base.metadata.create_all)
    maker = async_sessionmaker(engine, class_=AsyncSession, expire_on_commit=False)
    for mod in (start_mod, account_mod, products_mod, kb_mod, notify_mod):
        monkeypatch.setattr(mod, "SessionLocal", maker)
    try:
        yield maker
    finally:
        await engine.dispose()


async def test_start_refreshes_menu_without_rules_button(bound_db) -> None:
    async with bound_db() as s:
        s.add(User(telegram_id=9100, first_name="Old", language="fa"))
        s.add(Setting(key="rules_text", value="قانون"))
        await s.commit()
    msg = FM(FU(9100, "Old"))
    await start_mod.on_start(msg, FA, lang="fa", is_admin=False)
    # A fresh reply keyboard is always sent (so cached keyboards get replaced).
    assert msg.markups[0] is not None
    labels = [b.text for row in msg.markups[0].keyboard for b in row]
    assert FA("btn.rules") not in labels
    assert strip_menu_decoration(FA("btn.rules")) not in labels


async def test_account_opens_from_exact_production_label(bound_db) -> None:
    async with bound_db() as s:
        s.add(User(telegram_id=9200, first_name="Ada", username="ada", wallet_balance=1000))
        await s.commit()
    # Simulate the exact reply-button text a user taps.
    assert t("btn.account", "fa") in menu_texts("btn.account")
    msg = FM(FU(9200, "Ada"))
    await account_mod.on_account(msg, FA, FState(), lang="fa")
    assert FA("account.title") in msg.answers[0]

"""Telegram-admin bot statistics: service aggregates + the '📊 آمار ربات' section,
plus confirmation that '🧾 رسیدهای تایید نشده' stays a separate section."""
from __future__ import annotations

from datetime import datetime, timezone
from typing import Any

import pytest_asyncio
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker, create_async_engine
from sqlalchemy.pool import StaticPool

import app.bot.handlers.admin.panel as panel_mod
import app.bot.handlers.admin.stats as stats_mod
from app.bot.keyboards.admin import admin_main_menu
from app.core.permissions import Role
from app.i18n import t
from app.models import (
    Base,
    Order,
    Payment,
    PaymentMethod,
    Product,
    User,
    V2RayService,
    XuiServer,
)
from app.services import bot_stats_service

FA = lambda key, **p: t(key, "fa", **p)  # noqa: E731
NOW = datetime(2026, 6, 15, 12, 0, tzinfo=timezone.utc)


# --------------------------------------------------------------------------
# Fakes
# --------------------------------------------------------------------------
class FU:
    def __init__(self, uid):
        self.id = uid
        self.username = "adm"
        self.first_name = "A"
        self.last_name = "B"
        self.language_code = "fa"


class FM:
    def __init__(self, from_user=None, text=""):
        self.from_user = from_user
        self.text = text
        self.answers: list[str] = []
        self.markups: list[Any] = []
        self.edits: list[str] = []

    async def answer(self, text: str, **kwargs: Any) -> None:
        self.answers.append(text)
        self.markups.append(kwargs.get("reply_markup"))

    async def edit_text(self, text: str, **kwargs: Any) -> None:
        self.edits.append(text)
        self.markups.append(kwargs.get("reply_markup"))


class FC:
    def __init__(self, data, from_user, message):
        self.data = data
        self.from_user = from_user
        self.message = message
        self.alerts: list[str] = []

    async def answer(self, text: str = "", **kwargs: Any) -> None:
        if text:
            self.alerts.append(text)


class FState:
    def __init__(self):
        self._data: dict = {}
        self.state = None

    async def clear(self):
        self._data = {}
        self.state = None

    async def set_state(self, state):
        self.state = state

    async def get_state(self):
        return self.state

    async def update_data(self, **kw):
        self._data.update(kw)

    async def get_data(self):
        return dict(self._data)


def _inline_labels(markup) -> list[str]:
    if markup is None:
        return []
    return [b.text for row in markup.inline_keyboard for b in row]


@pytest_asyncio.fixture
async def db(monkeypatch):
    engine = create_async_engine("sqlite+aiosqlite://", poolclass=StaticPool,
                                 connect_args={"check_same_thread": False})
    async with engine.begin() as conn:
        await conn.run_sync(Base.metadata.create_all)
    maker = async_sessionmaker(engine, class_=AsyncSession, expire_on_commit=False)
    monkeypatch.setattr(stats_mod, "SessionLocal", maker)
    monkeypatch.setattr(panel_mod, "SessionLocal", maker)
    try:
        yield maker
    finally:
        await engine.dispose()


async def _seed(maker) -> None:
    """Two delivered sales (one renewal, one with an active service + coupon),
    three users, two active gateways + one inactive, two approved payments,
    one panel."""
    async with maker() as s:
        s.add_all([
            User(telegram_id=1, first_name="u1", wallet_balance=1000),
            User(telegram_id=2, first_name="u2", wallet_balance=2000),
            User(telegram_id=3, first_name="u3", wallet_balance=0),  # no purchase
        ])
        p = Product(type="v2ray", title="Pro", price=50000, is_active=True, is_hidden=False)
        s.add(p)
        await s.commit()
        # Delivered sale #1: has a coupon + discount + an active v2ray service.
        o1 = Order(order_number="DC-1", user_id=1, product_id=p.id, amount=55000,
                   discount_amount=5000, final_amount=50000, status="delivered",
                   coupon_id=1, coupon_code="OFF", delivered_at=NOW)
        # Delivered sale #2: a renewal.
        o2 = Order(order_number="DC-2", user_id=2, product_id=p.id, amount=30000,
                   final_amount=30000, status="delivered", action_type="renew_service",
                   delivered_at=NOW)
        # A non-delivered order must never count.
        o3 = Order(order_number="DC-3", user_id=2, product_id=p.id, amount=99000,
                   final_amount=99000, status="pending_payment")
        s.add_all([o1, o2, o3])
        await s.commit()
        s.add(V2RayService(
            user_id=1, order_id=o1.id, product_id=p.id, xui_server_id=1,
            xui_inbound_id=1, client_email="c@x", client_uuid="u", status="active"))
        s.add_all([
            PaymentMethod(code="wallet", title="کیف پول", method_type="wallet",
                          is_active=True, sort_order=0),
            PaymentMethod(code="card", title="کارت به کارت", method_type="manual_receipt",
                          is_active=True, sort_order=1),
            PaymentMethod(code="og", title="درگاه", method_type="online_gateway",
                          is_active=False, sort_order=2),  # inactive → hidden in stats
        ])
        s.add_all([
            Payment(order_id=o1.id, user_id=1, amount=50000, method="wallet",
                    provider_name="wallet", status="approved", approved_at=NOW),
            Payment(order_id=o2.id, user_id=2, amount=30000, method="card_to_card",
                    provider_name="card", status="approved", approved_at=NOW),
        ])
        s.add(XuiServer(name="s1", base_url="http://x", is_active=True))
        await s.commit()


# ==========================================================================
# Service: aggregates
# ==========================================================================
async def test_get_bot_stats_all_fields(db) -> None:
    await _seed(db)
    async with db() as s:
        st = await bot_stats_service.get_bot_stats(s, "all")
    assert st["total_users"] == 3
    assert st["users_with_purchase"] == 2
    assert st["total_users_balance"] == 3000
    assert st["total_sales_count"] == 2
    assert st["total_sales_amount"] == 80000
    assert st["active_services_sales_count"] == 1
    assert st["active_services_sales_amount"] == 50000
    assert st["total_renew_amount"] == 30000
    assert st["total_discount_amount"] == 5000
    assert st["discount_usage_count"] == 1
    assert st["conversion_rate"] == round(2 / 3 * 100, 2)
    assert st["average_purchase_per_customer"] == 40000
    assert st["renew_percent_from_sales"] == 37.5
    assert st["total_panels"] == 1
    # Features not modelled yet.
    assert st["total_test_accounts"] == 0
    assert st["total_resellers"] == 0
    assert st["n_resellers_count"] == 0
    assert st["n2_resellers_count"] == 0


async def test_stats_empty_db_no_zero_division(db) -> None:
    async with db() as s:
        st = await bot_stats_service.get_bot_stats(s, "all")
    assert st["conversion_rate"] == 0.0            # 0 users → guarded
    assert st["average_purchase_per_customer"] == 0  # 0 buyers → guarded
    assert st["renew_percent_from_sales"] == 0.0
    assert st["predicted_monthly_income"] == 0     # no data → 0


async def test_gateway_stats_hides_inactive(db) -> None:
    await _seed(db)
    async with db() as s:
        gw = await bot_stats_service.get_gateway_stats(s)
    codes = {g["code"] for g in gw}
    assert codes == {"wallet", "card"}             # inactive "og" excluded
    by_code = {g["code"]: g for g in gw}
    assert by_code["wallet"]["successful_payments_count"] == 1
    assert by_code["wallet"]["successful_payments_amount"] == 50000
    assert by_code["card"]["successful_payments_amount"] == 30000


async def test_product_comparison_only_products_with_sales(db) -> None:
    await _seed(db)
    async with db() as s:
        rows = await bot_stats_service.get_product_sales_comparison(s)
    assert len(rows) == 1
    assert rows[0]["product_name"] == "Pro"
    assert rows[0]["sales_count"] == 2
    assert rows[0]["sales_amount"] == 80000


async def test_product_comparison_empty_range(db) -> None:
    await _seed(db)
    # A window entirely before the seeded sales → no rows.
    start = datetime(2020, 1, 1, tzinfo=timezone.utc)
    end = datetime(2020, 2, 1, tzinfo=timezone.utc)
    async with db() as s:
        rows = await bot_stats_service.get_product_sales_comparison(s, start, end)
    assert rows == []


def test_calculate_range_presets() -> None:
    assert bot_stats_service.calculate_range("all") == (None, None)
    s, e = bot_stats_service.calculate_range("last_hour")
    assert s is not None and e is not None and (e - s).seconds == 3600
    for preset in ("today", "yesterday", "current_month", "previous_month"):
        s, e = bot_stats_service.calculate_range(preset)
        assert s is not None and e is not None and s < e


def test_jalali_parsing() -> None:
    # 1404/01/01 is the Persian new year → 2025-03-21.
    d = bot_stats_service.parse_jalali_date("1404/01/01")
    assert d == datetime(2025, 3, 21, tzinfo=timezone.utc)
    # Persian digits + end-of-day (exclusive next midnight).
    e = bot_stats_service.parse_jalali_date("۱۴۰۴/۰۱/۰۱", end_of_day=True)
    assert e == datetime(2025, 3, 22, tzinfo=timezone.utc)
    assert bot_stats_service.parse_jalali_date("nonsense") is None
    assert bot_stats_service.parse_jalali_date("1404/13/40") is None  # impossible date


# ==========================================================================
# Bot section: menu + handlers
# ==========================================================================
def test_admin_menu_has_stats_and_pending_separately() -> None:
    for lang in ("fa", "en"):
        labels = [b.text for row in admin_main_menu(lang).keyboard for b in row]
        assert t("btn.admin.stats", lang) in labels
        assert t("btn.admin.pending", lang) in labels
    # They are genuinely different sections (different callbacks/handlers).
    assert stats_mod.CB_STATS != panel_mod.CB_ADMIN_PENDING
    assert stats_mod.STATS_TEXTS.isdisjoint(panel_mod.PENDING_TEXTS)


async def test_stats_button_renders_for_admin(db) -> None:
    await _seed(db)
    msg = FM(FU(1))
    await stats_mod.on_stats_button(msg, FA, FState(), lang="fa", role=Role.OWNER)
    body = msg.answers[0]
    assert "📊 آمار کلی ربات" in body
    assert "تعداد کل کاربران: 3" in body
    assert "کیف پول" in body           # a gateway row rendered
    labels = _inline_labels(msg.markups[0])
    assert FA("admin.stats.btn.today") in labels
    assert FA("admin.stats.btn.products") in labels


async def test_stats_denied_for_support(db) -> None:
    msg = FM(FU(1))
    await stats_mod.on_stats_button(msg, FA, FState(), lang="fa", role=Role.SUPPORT)
    assert msg.answers == [FA("admin.stats.denied")]


async def test_stats_ignores_non_admin(db) -> None:
    # "داشبورد" overlaps a common word; a non-admin must fall through silently.
    msg = FM(FU(9), text="داشبورد")
    await stats_mod.on_stats_button(msg, FA, FState(), lang="fa", role=None)
    assert msg.answers == []


async def test_stats_range_callback_edits(db) -> None:
    await _seed(db)
    msg = FM(FU(1))
    cb = FC(f"{stats_mod.CB_STATS}today", FU(1), msg)
    await stats_mod.on_stats_cb(cb, FA, FState(), lang="fa", role=Role.OWNER)
    assert msg.edits and "📊 آمار کلی ربات" in msg.edits[-1]


async def test_stats_custom_date_flow(db) -> None:
    await _seed(db)
    state = FState()
    # Tap "custom" → asks for the start date and arms the FSM.
    cb = FC(f"{stats_mod.CB_STATS}custom", FU(1), FM(FU(1)))
    await stats_mod.on_stats_cb(cb, FA, state, lang="fa", role=Role.OWNER)
    assert state.state == stats_mod.StatsStates.waiting_stats_start_date

    # Send start date → asks for end date.
    m1 = FM(FU(1), text="1404/01/01")
    await stats_mod.on_stats_start_date(m1, FA, state, lang="fa")
    assert state.state == stats_mod.StatsStates.waiting_stats_end_date
    assert m1.answers == [FA("admin.stats.ask_end")]

    # Send end date → renders stats + clears state.
    m2 = FM(FU(1), text="1404/12/29")
    await stats_mod.on_stats_end_date(m2, FA, state, lang="fa")
    assert state.state is None
    assert m2.answers and "📊 آمار کلی ربات" in m2.answers[0]


async def test_stats_custom_invalid_date(db) -> None:
    state = FState()
    await state.set_state(stats_mod.StatsStates.waiting_stats_start_date)
    m = FM(FU(1), text="not-a-date")
    await stats_mod.on_stats_start_date(m, FA, state, lang="fa")
    assert m.answers == [FA("admin.stats.bad_date")]
    assert state.state == stats_mod.StatsStates.waiting_stats_start_date  # still waiting


async def test_stats_custom_end_before_start(db) -> None:
    state = FState()
    await state.update_data(stats_start=datetime(2025, 6, 1, tzinfo=timezone.utc).isoformat())
    await state.set_state(stats_mod.StatsStates.waiting_stats_end_date)
    m = FM(FU(1), text="1404/01/01")  # 2025-03-21, before the start
    await stats_mod.on_stats_end_date(m, FA, state, lang="fa")
    assert m.answers == [FA("admin.stats.end_before_start")]
    assert state.state == stats_mod.StatsStates.waiting_stats_start_date


async def test_product_comparison_presets(db) -> None:
    await _seed(db)
    for preset in ("7d", "30d", "current_month"):
        msg = FM(FU(1))
        cb = FC(f"{stats_mod.CB_STATS_PRODUCTS}{preset}", FU(1), msg)
        await stats_mod.on_products_cb(cb, FA, FState(), lang="fa", role=Role.OWNER)
        assert msg.edits and "مقایسه فروش محصولات" in msg.edits[-1]


async def test_products_menu_and_back(db) -> None:
    await _seed(db)
    msg = FM(FU(1))
    cb = FC(f"{stats_mod.CB_STATS}products", FU(1), msg)
    await stats_mod.on_stats_cb(cb, FA, FState(), lang="fa", role=Role.OWNER)
    assert FA("admin.stats.products.btn.7d") in _inline_labels(msg.markups[-1])

    # Back to admin menu shows the reply keyboard.
    back = FC(stats_mod.CB_MENU_BACK, FU(1), FM(FU(1)))
    await stats_mod.on_stats_back(back, FA, FState(), lang="fa", role=Role.OWNER)
    assert back.message.answers  # a message with the admin menu was sent


# ==========================================================================
# Pending receipts: separate section
# ==========================================================================
async def test_pending_button_separate_from_stats(db) -> None:
    await _seed(db)
    msg = FM(FU(1))
    await panel_mod.on_admin_pending_button(msg, FA, lang="fa", role=Role.OWNER)
    # Renders the pending-receipts view (empty here) — its own section.
    assert msg.answers and msg.answers[0] == FA("admin.fin.pending_empty")


async def test_pending_denied_without_permission(db) -> None:
    msg = FM(FU(1))
    await panel_mod.on_admin_pending_button(msg, FA, lang="fa", role=Role.SUPPORT)
    assert msg.answers == [FA("admin.not_authorized")]

"""Bot message formatting: the shared helper + the reformatted high-impact
messages (invoice, manual receipt, account) render in a large, readable, HTML-safe
style."""
from __future__ import annotations

from typing import Any

import pytest_asyncio
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker, create_async_engine
from sqlalchemy.pool import StaticPool

import app.bot.handlers.user.account as account_mod
import app.bot.keyboards.user as kb_mod
from app.bot.utils import message_format as mf
from app.i18n import fa, t
from app.models import Base, User

FA = lambda key, **p: t(key, "fa", **p)  # noqa: E731
DIVIDER = mf.DIVIDER


# ==========================================================================
# Helper unit tests
# ==========================================================================
def test_format_money() -> None:
    assert mf.format_money(1234000) == "1,234,000 تومان"
    assert mf.format_money(0) == "0 تومان"
    assert mf.format_money(None) == "0 تومان"


def test_format_gb() -> None:
    assert mf.format_gb(50) == "50 گیگابایت"
    assert mf.format_gb(0) == "نامحدود"          # 0 → unlimited
    assert mf.format_gb(2 * 1024 ** 3) == "2 گیگابایت"  # bytes → GB


def test_safe_code_escapes_html() -> None:
    assert mf.safe_code("<a>&'\"") == "<code>&lt;a&gt;&amp;&#x27;&quot;</code>"
    assert mf.esc("<b>&") == "&lt;b&gt;&amp;"


def test_render_big_message_structure() -> None:
    msg = mf.render_big_message(
        "🧾 Title",
        sections=[("Label A:", "Value A"), ("Empty:", ""), ("Label B:", "Value B")],
        footer="Footer note.",
    )
    lines = msg.split("\n")
    assert lines[0] == "🧾 Title"
    assert lines[1] == DIVIDER
    assert "Label A:" in msg and "Value A" in msg
    assert "Empty:" not in msg          # blank values are dropped
    assert msg.rstrip().endswith("Footer note.")
    assert msg.count(DIVIDER) == 2       # header + footer rules


def test_section_title_and_divider() -> None:
    assert mf.divider() == DIVIDER
    assert mf.section_title("X") == f"X\n{DIVIDER}"


# ==========================================================================
# Invoice (پیش‌فاکتور)
# ==========================================================================
class _Prod:
    def __init__(self, **kw):
        self.type = kw.get("type", "v2ray")
        self.title = kw.get("title", "Gold VPN")
        self.price = kw.get("price", 250000)
        self.duration_days = kw.get("duration_days", 30)
        self.traffic_gb = kw.get("traffic_gb", 50)
        self.ip_limit = kw.get("ip_limit", None)
        self.description = kw.get("description", None)


def test_invoice_is_readable() -> None:
    from app.bot.handlers.user.products import build_invoice_lines
    body = "\n".join(build_invoice_lines(_Prod(), "وی‌پی‌ان", "سرور آلمان", "fa"))
    assert FA("products.invoice.title") in body   # title line
    assert DIVIDER in body                          # divider
    assert "250,000 تومان" in body                  # readable price
    assert "30 روز" in body                          # duration
    assert "50 گیگابایت" in body                     # traffic
    # Label and value are on separate lines (readable, not one dense line).
    assert f"{FA('products.lbl.product')}\nGold VPN" in body


def test_invoice_escapes_special_chars() -> None:
    from app.bot.handlers.user.products import build_invoice_lines
    body = "\n".join(build_invoice_lines(_Prod(title="a<b>&c"), "cat", None, "fa"))
    assert "a&lt;b&gt;&amp;c" in body and "a<b>&c" not in body  # no raw HTML


# ==========================================================================
# Manual card-to-card receipt
# ==========================================================================
class _Order:
    order_number = "DC-1024"
    amount = 300000
    discount_amount = 0
    final_amount = 250000
    coupon_code = None


def test_manual_receipt_is_readable() -> None:
    from app.bot.handlers.user.orders import _payment_instruction_lines
    cfg = {"card_number": "6037-0000-1111-2222", "card_owner": "علی",
           "sheba_number": "IR12", "payment_instructions": ""}
    body = "\n".join(_payment_instruction_lines(_Order(), _Prod(), cfg, FA, "PAY-XY12"))
    assert FA("purchase.manual_title") in body
    assert DIVIDER in body
    assert "250,000 تومان" in body                        # amount
    assert "<code>6037-0000-1111-2222</code>" in body     # card as tap-to-copy
    assert "<code>PAY-XY12</code>" in body                # tracking code
    assert "DC-1024" in body                              # order number kept


# ==========================================================================
# Account page + admin stats + parse-mode safety
# ==========================================================================
@pytest_asyncio.fixture
async def db(monkeypatch):
    engine = create_async_engine("sqlite+aiosqlite://", poolclass=StaticPool,
                                 connect_args={"check_same_thread": False})
    async with engine.begin() as conn:
        await conn.run_sync(Base.metadata.create_all)
    maker = async_sessionmaker(engine, class_=AsyncSession, expire_on_commit=False)
    for mod in (account_mod, kb_mod):
        monkeypatch.setattr(mod, "SessionLocal", maker)
    try:
        yield maker
    finally:
        await engine.dispose()


class FU:
    def __init__(self, uid=7001):
        self.id = uid
        self.username = "ada"
        self.first_name = "Ada"
        self.last_name = "Lovelace"


class FM:
    def __init__(self, from_user=None):
        self.from_user = from_user
        self.answers: list[str] = []

    async def answer(self, text: str, **kwargs: Any) -> None:
        self.answers.append(text)


class FState:
    def __init__(self):
        self.state = None

    async def clear(self):
        self.state = None


async def test_account_page_uses_title_and_divider(db) -> None:
    async with db() as s:
        s.add(User(telegram_id=7001, first_name="Ada", last_name="Lovelace",
                   username="ada", wallet_balance=12345))
        await s.commit()
    msg = FM(FU())
    await account_mod.on_account(msg, FA, FState(), lang="fa")
    body = msg.answers[0]
    assert FA("account.title") in body
    assert DIVIDER in body                 # readable big-message divider
    assert "12,345" in body                # wallet balance still present


def test_admin_stats_body_is_big_style() -> None:
    # The admin '📊 آمار ربات' overview keeps its readable divider layout.
    assert DIVIDER in fa.CATALOG["admin.stats.body"]
    assert "📊 آمار کلی ربات" in fa.CATALOG["admin.stats.body"]


def test_no_parse_mode_break_on_special_chars() -> None:
    # A value full of HTML metacharacters is fully escaped by the helpers, so an
    # HTML-parse-mode send can never be corrupted by user input.
    nasty = "<script>alert('x')</script> & \"q\""
    assert "<" not in mf.esc(nasty) and ">" not in mf.esc(nasty)
    code = mf.safe_code(nasty)
    assert code.startswith("<code>") and code.endswith("</code>")
    assert "<script>" not in code

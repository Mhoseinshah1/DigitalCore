"""Bot UX phase: category browse + invoice, wallet receipt fix, account page,
rules-on-start, and the restructured main menu."""
from __future__ import annotations

from typing import Any

import pytest_asyncio
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker, create_async_engine
from sqlalchemy.pool import StaticPool

import app.bot.handlers.user.account as account_mod
import app.bot.handlers.user.orders as orders_mod
import app.bot.handlers.user.products as products_mod
import app.bot.handlers.user.start as start_mod
import app.bot.handlers.user.wallet as wallet_mod
import app.bot.keyboards.user as kb_mod
import app.bot.notifications as notify_mod
from app.i18n import t
from app.models import Base, Product, Setting, User
from app.schemas.product import ProductCreate
from app.services import payment_service, product_category_service, product_service, wallet_service

FA = lambda key, **p: t(key, "fa", **p)  # noqa: E731


class FU:
    def __init__(self, uid, username="u", first_name="F", last_name="L"):
        self.id = uid
        self.username = username
        self.first_name = first_name
        self.last_name = last_name
        self.language_code = "fa"


class FM:
    def __init__(self, from_user=None, photo=None, document=None, text=""):
        self.from_user = from_user
        self.photo = photo
        self.document = document
        self.text = text
        self.answers: list[str] = []
        self.markups: list[Any] = []

    async def answer(self, text: str, **kwargs: Any) -> None:
        self.answers.append(text)
        self.markups.append(kwargs.get("reply_markup"))

    async def delete(self) -> None:
        pass


class FC:
    def __init__(self, data, from_user, message):
        self.data = data
        self.from_user = from_user
        self.message = message
        self.alerts: list[str] = []

    async def answer(self, text: str = "", **kwargs: Any) -> None:
        if text:
            self.alerts.append(text)


class FPhoto:
    def __init__(self, file_id, file_size):
        self.file_id = file_id
        self.file_size = file_size


class FDoc:
    def __init__(self, file_id, file_name, mime_type, file_size):
        self.file_id = file_id
        self.file_name = file_name
        self.mime_type = mime_type
        self.file_size = file_size


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


class FBot:
    async def send_message(self, *a, **k):
        pass

    async def send_photo(self, *a, **k):
        pass

    async def send_document(self, *a, **k):
        pass


def _btn_texts(markup) -> list[str]:
    if markup is None:
        return []
    return [b.text for row in markup.inline_keyboard for b in row]


@pytest_asyncio.fixture
async def db(monkeypatch, tmp_path):
    engine = create_async_engine("sqlite+aiosqlite://", poolclass=StaticPool,
                                 connect_args={"check_same_thread": False})
    async with engine.begin() as conn:
        await conn.run_sync(Base.metadata.create_all)
    maker = async_sessionmaker(engine, class_=AsyncSession, expire_on_commit=False)
    for mod in (products_mod, orders_mod, wallet_mod, account_mod, start_mod, kb_mod, notify_mod):
        monkeypatch.setattr(mod, "SessionLocal", maker)
    monkeypatch.setattr(payment_service, "RECEIPTS_ROOT", tmp_path)

    async def fake_download(bot, file_id):
        return b"\x89PNG\r\n\x1a\n" + b"receipt-bytes"
    monkeypatch.setattr(orders_mod, "_download_telegram_file", fake_download)
    try:
        yield maker
    finally:
        await engine.dispose()


# --------------------------------------------------------------------------
# Category browse + invoice
# --------------------------------------------------------------------------
async def _seed_two_categories(maker):
    async with maker() as s:
        s.add(Setting(key="card_number", value="6037-0000-0000-0000"))
        s.add(Setting(key="coupons_enabled", value="false"))
        await s.commit()
        c1 = await product_category_service.create(s, title="لایسنس‌ها", sort_order=0)
        c2 = await product_category_service.create(s, title="وی‌پی‌ان", sort_order=1)
        await s.commit()
        await product_service.create(s, ProductCreate(
            type="license", title="Win Key", price=50000, category_id=c1.id))
        await product_service.create(s, ProductCreate(
            type="license", title="Uncategorised", price=9000))
        await s.commit()
        return c1.id, c2.id


async def test_products_shows_categories_first(db) -> None:
    c1, _c2 = await _seed_two_categories(db)
    msg = FM(FU(5001))
    await products_mod.on_products(msg, FA, FState(), lang="fa")
    assert FA("products.category.choose") in msg.answers[0]
    labels = _btn_texts(msg.markups[0])
    assert "لایسنس‌ها" in labels
    assert FA("products.category.other") in labels  # uncategorised fallback


async def test_open_category_lists_products(db) -> None:
    c1, _c2 = await _seed_two_categories(db)
    msg = FM(FU(5002))
    cb = FC(f"{products_mod.CB_CAT}{c1}", FU(5002), msg)
    await products_mod.on_category_open(cb, FA, lang="fa")
    body = "\n".join(msg.answers)
    assert "Win Key" in body
    assert "Uncategorised" not in body  # that product is in the Other group


async def test_invoice_shows_prefactor_and_payment_buttons(db) -> None:
    await _seed_two_categories(db)
    async with db() as s:
        p = (await product_service.list_for_user(s))[0]
    msg = FM(FU(5003))
    cb = FC(f"{products_mod.CB_DETAIL}{p.id}", FU(5003), msg)
    await products_mod.on_product_detail(cb, FA, lang="fa")
    body = msg.answers[0]
    assert FA("products.invoice.title") in body
    labels = _btn_texts(msg.markups[0])
    assert FA("btn.pay_wallet") in labels
    assert FA("btn.pay_card") in labels
    # Gateway is off by default → no gateway button.
    assert FA("btn.pay_gateway") not in labels


async def test_invoice_gateway_button_when_enabled(db) -> None:
    await _seed_two_categories(db)
    async with db() as s:
        s.add(Setting(key="online_gateway_enabled", value="true"))
        await s.commit()
        p = (await product_service.list_for_user(s))[0]
    msg = FM(FU(5004))
    cb = FC(f"{products_mod.CB_DETAIL}{p.id}", FU(5004), msg)
    await products_mod.on_product_detail(cb, FA, lang="fa")
    assert FA("btn.pay_gateway") in _btn_texts(msg.markups[0])
    # Tapping it shows the coming-soon placeholder.
    gcb = FC(f"{orders_mod.CB_INV_GATEWAY}{p.id}", FU(5004), FM(FU(5004)))
    await orders_mod.on_inv_gateway(gcb, FA, lang="fa")
    assert gcb.alerts and FA("gateway.coming_soon") in gcb.alerts[0]


async def test_single_group_falls_back_to_flat_list(db) -> None:
    async with db() as s:
        await product_service.create(s, ProductCreate(
            type="license", title="Only Product", price=1000))
        await s.commit()
    msg = FM(FU(5005))
    await products_mod.on_products(msg, FA, FState(), lang="fa")
    # No real categories → straight to the product list.
    assert "Only Product" in "\n".join(msg.answers)


# --------------------------------------------------------------------------
# Wallet receipt fix
# --------------------------------------------------------------------------
async def _seed_topup(maker, uid) -> tuple[int, int]:
    async with maker() as s:
        user = User(telegram_id=uid, first_name="B")
        s.add(user)
        await s.commit()
        topup = await wallet_service.create_topup_request(s, user.id, 50000)
        await s.commit()
        return topup.id, 50000


async def test_wallet_receipt_photo_detailed_confirmation(db) -> None:
    topup_id, amount = await _seed_topup(db, 6001)
    state = FState()
    await state.set_state(wallet_mod.WalletStates.waiting_for_topup_receipt)
    await state.update_data(topup_id=topup_id)
    msg = FM(FU(6001), photo=[FPhoto("f", 2048)])
    await wallet_mod.on_topup_receipt(msg, FBot(), FA, state, lang="fa")
    body = "\n".join(msg.answers)
    assert f"{amount:,}" in body            # amount shown
    assert str(topup_id) in body            # request number shown
    # FSM moved forward so the user is not stuck.
    assert state.state is None
    # The top-up moved to waiting_admin.
    async with db() as s:
        topups = await wallet_service.list_topups(s, status="waiting_admin")
    assert any(tp.id == topup_id for tp in topups)


async def test_wallet_receipt_pdf_document_accepted(db) -> None:
    topup_id, _amount = await _seed_topup(db, 6002)
    state = FState()
    await state.set_state(wallet_mod.WalletStates.waiting_for_topup_receipt)
    await state.update_data(topup_id=topup_id)
    msg = FM(FU(6002), document=FDoc("f", "rcpt.pdf", "application/pdf", 4096))
    await wallet_mod.on_topup_receipt(msg, FBot(), FA, state, lang="fa")
    assert any(str(topup_id) in a for a in msg.answers)


async def test_wallet_receipt_invalid_type_rejected(db) -> None:
    topup_id, _amount = await _seed_topup(db, 6003)
    state = FState()
    await state.set_state(wallet_mod.WalletStates.waiting_for_topup_receipt)
    await state.update_data(topup_id=topup_id)
    msg = FM(FU(6003), document=FDoc("f", "rcpt.exe", "application/octet-stream", 4096))
    await wallet_mod.on_topup_receipt(msg, FBot(), FA, state, lang="fa")
    assert msg.answers == [FA("wallet.topup.receipt.unsupported_type")]


async def test_wallet_receipt_text_guidance(db) -> None:
    topup_id, _amount = await _seed_topup(db, 6004)
    msg = FM(FU(6004), text="سلام")
    await wallet_mod.on_topup_receipt_wrong(msg, FA)
    assert msg.answers == [FA("wallet.topup.receipt_required_file")]


async def test_wallet_receipt_catch_all_never_stuck(db) -> None:
    # A non-photo, non-text update (e.g. a sticker) still gets a reply.
    msg = FM(FU(6005))
    await wallet_mod.on_topup_receipt_catch_all(msg, FA)
    assert msg.answers == [FA("wallet.topup.receipt_required_file")]


# --------------------------------------------------------------------------
# Account page
# --------------------------------------------------------------------------
async def test_account_summary_and_buttons(db) -> None:
    async with db() as s:
        s.add(User(telegram_id=7001, first_name="Ada", last_name="Lovelace",
                   username="ada", wallet_balance=12345))
        await s.commit()
    msg = FM(FU(7001, username="ada", first_name="Ada", last_name="Lovelace"))
    await account_mod.on_account(msg, FA, FState(), lang="fa")
    body = msg.answers[0]
    assert FA("account.title") in body
    assert "7001" in body                # numeric id
    assert "12,345" in body              # wallet balance
    labels = _btn_texts(msg.markups[0])
    assert FA("btn.wallet") in labels
    assert FA("btn.my_orders") in labels
    assert FA("btn.support") in labels


# --------------------------------------------------------------------------
# Rules on /start + menu structure
# --------------------------------------------------------------------------
async def test_start_shows_rules_and_menu_without_rules_button(db) -> None:
    async with db() as s:
        s.add(User(telegram_id=8001, first_name="Old", language="fa"))
        s.add(Setting(key="rules_text", value="قانون یک"))
        await s.commit()
    msg = FM(FU(8001, first_name="Old"))
    await start_mod.on_start(msg, FA, lang="fa", is_admin=False)
    # First message = greeting + menu; a later message carries the rules text.
    assert any("قانون یک" in a for a in msg.answers)
    # The reply menu never contains the قوانین button.
    menu = msg.markups[0]
    menu_labels = [b.text for row in menu.keyboard for b in row]
    assert FA("btn.rules") not in menu_labels
    assert FA("btn.account") in menu_labels


async def test_start_rules_admin_hint(db) -> None:
    async with db() as s:
        s.add(User(telegram_id=8002, first_name="Adm", language="fa"))
        s.add(Setting(key="rules_text", value="قانون"))
        await s.commit()
    msg = FM(FU(8002, first_name="Adm"))
    await start_mod.on_start(msg, FA, lang="fa", is_admin=True)
    assert any(FA("rules.admin_change_hint") in a for a in msg.answers)


async def test_menu_uses_configured_license_title(db) -> None:
    async with db() as s:
        s.add(User(telegram_id=8003, first_name="U", language="fa"))
        s.add(Setting(key="license_section_title", value="اپل آیدی‌های من"))
        await s.commit()
    msg = FM(FU(8003, first_name="U"))
    await start_mod.on_start(msg, FA, lang="fa", is_admin=False)
    menu_labels = [b.text for row in msg.markups[0].keyboard for b in row]
    assert "اپل آیدی‌های من" in menu_labels

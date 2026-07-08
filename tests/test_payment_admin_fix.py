"""Runtime-fix regression tests (payment methods / receipts / admin bot panel).

Covers the owner-reported bugs fixed in this change:
  * Bug 1  — every active payment method is offered (table-driven + settings
             fallback), and unimplemented gateways show a safe notice.
  * Bug 2  — a receipt upload never dead-ends: storage failures and unexpected
             errors both produce a clear reply, and the FSM is not stranded.
  * Bug 3  — the card-to-card screen carries copy-amount / copy-card / paid /
             back buttons (native copy_text when supported, echo fallback else).
  * Bug 4-6 / 8 / 9 — the admin dashboard / users / financial reply-buttons all
             respond, no menu button is dead, and /admin_debug leaks no secrets.
"""
from __future__ import annotations

from typing import Any

import pytest_asyncio
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker, create_async_engine
from sqlalchemy.pool import StaticPool

import app.bot.handlers.admin.fallback as fallback_mod
import app.bot.handlers.admin.panel as panel_mod
import app.bot.handlers.user.orders as orders_mod
import app.bot.handlers.user.wallet as wallet_mod
import app.bot.notifications as notify_mod
from app.bot.keyboards.admin import admin_main_menu
from app.bot.payment_ui import (
    UNIMPLEMENTED_GATEWAY_TYPES,
    build_copy_button,
    copy_text_supported,
    manual_receipt_keyboard,
    method_label,
)
from app.core.permissions import Role
from app.i18n import t
from app.models import Base, PaymentMethod, Product, Setting, User
from app.services import payment_core_service, payment_service, wallet_service

FA = lambda key, **p: t(key, "fa", **p)  # noqa: E731


# --------------------------------------------------------------------------
# Fakes (mirrors tests/test_bot_ux.py)
# --------------------------------------------------------------------------
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

    async def get_state(self):
        return self.state

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

    async def get_file(self, *a, **k):  # pragma: no cover - not reached
        raise AssertionError("get_file should be monkeypatched")


def _labels(markup) -> list[str]:
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
    for mod in (orders_mod, wallet_mod, panel_mod, notify_mod):
        monkeypatch.setattr(mod, "SessionLocal", maker)
    monkeypatch.setattr(payment_service, "RECEIPTS_ROOT", tmp_path)

    async def fake_download(bot, file_id):
        return b"\x89PNG\r\n\x1a\n" + b"receipt-bytes"
    monkeypatch.setattr(orders_mod, "_download_telegram_file", fake_download)
    try:
        yield maker
    finally:
        await engine.dispose()


async def _seed_methods(maker, rows: list[dict]) -> None:
    async with maker() as s:
        for r in rows:
            s.add(PaymentMethod(**r))
        await s.commit()


async def _seed_product(maker, *, price=120000, card="6037-0000-0000-0000") -> int:
    async with maker() as s:
        s.add(Setting(key="card_number", value=card))
        s.add(Setting(key="coupons_enabled", value="false"))
        p = Product(type="license", title="Gold Key", price=price,
                    is_active=True, is_hidden=False)
        s.add(p)
        await s.commit()
        return p.id


# ==========================================================================
# Bug 1 — payment-method resolver
# ==========================================================================
async def test_resolver_db_path_offers_every_active_method(db) -> None:
    async with db() as s:
        s.add(Setting(key="custom_gateway_enabled", value="true"))
        await s.commit()
    await _seed_methods(db, [
        dict(code="wallet", title="Wallet", method_type="wallet", sort_order=0),
        dict(code="card", title="Card", method_type="manual_receipt", sort_order=1),
        dict(code="cg", title="Custom", method_type="custom_gateway", sort_order=2),
        dict(code="og", title="Online", method_type="online_gateway",
             sort_order=3, is_active=False),  # inactive → filtered
    ])
    async with db() as s:
        types = await payment_core_service.resolve_method_types(s, 50000, user=None)
    # All active rows, in sort order; the inactive one is dropped (not hidden by
    # accident — it is genuinely deactivated).
    assert types == ["wallet", "manual_receipt", "custom_gateway"]


async def test_resolver_respects_amount_window(db) -> None:
    await _seed_methods(db, [
        dict(code="wallet", title="Wallet", method_type="wallet", sort_order=0),
        dict(code="crypto", title="Crypto", method_type="crypto",
             sort_order=1, min_amount=100000),  # too pricey for a 50k order
    ])
    async with db() as s:
        types = await payment_core_service.resolve_method_types(s, 50000, user=None)
    assert types == ["wallet"]


async def test_resolver_settings_fallback_when_table_empty(db) -> None:
    # No PaymentMethod rows (older/fresh DB where the 0021 seed never ran):
    # the resolver falls back to the settings-derived defaults.
    async with db() as s:
        s.add(Setting(key="card_number", value="6037-1"))
        await s.commit()
        types = await payment_core_service.resolve_method_types(s, 10000, user=None)
    assert types == ["wallet", "manual_receipt"]  # gateways off by default


async def test_resolver_fallback_excludes_wallet_for_topup(db) -> None:
    async with db() as s:
        s.add(Setting(key="card_number", value="6037-1"))
        await s.commit()
        types = await payment_core_service.resolve_method_types(
            s, 10000, user=None, exclude_wallet=True)
    assert "wallet" not in types
    assert types == ["manual_receipt"]


async def test_offer_methods_shows_picker_for_every_type(db) -> None:
    pid = await _seed_product(db)
    await _seed_methods(db, [
        dict(code="wallet", title="Wallet", method_type="wallet", sort_order=0),
        dict(code="card", title="Card", method_type="manual_receipt", sort_order=1),
        dict(code="og", title="Online", method_type="online_gateway", sort_order=2),
    ])
    async with db() as s:
        s.add(Setting(key="online_gateway_enabled", value="true"))
        await s.commit()
    msg = FM(FU(9101))
    await orders_mod.on_buy(FC(f"ubuy:{pid}", FU(9101), msg), FBot(), FA, FState(), lang="fa")
    labels = _labels(msg.markups[-1])
    assert FA("btn.pay_wallet") in labels
    assert FA("btn.pay_card") in labels
    assert FA("btn.pay_gateway") in labels


async def test_extra_gateway_tap_shows_not_connected(db) -> None:
    # A not-yet-wired gateway must show the safe notice, never dead-end.
    cb = FC(f"{orders_mod.CB_INV_GW2}custom_gateway:5", FU(9102), FM(FU(9102)))
    await orders_mod.on_inv_extra_gateway(cb, FA, lang="fa")
    assert cb.alerts == [FA("gateway.not_connected")]


async def test_all_unimplemented_types_have_a_label(db) -> None:
    # Every gateway type we might surface has a real (non-key) button label.
    for mtype in UNIMPLEMENTED_GATEWAY_TYPES:
        label = method_label(FA, mtype)
        assert label and not label.startswith("btn.pay_")


# ==========================================================================
# Bug 2 — receipt upload never stuck
# ==========================================================================
async def _buy_and_arm_receipt(db, uid) -> tuple[int, FState]:
    pid = await _seed_product(db)
    await _seed_methods(db, [
        dict(code="card", title="Card", method_type="manual_receipt", sort_order=0),
    ])
    msg = FM(FU(uid))
    state = FState()
    # Single method (card) → straight to the card screen (arms the receipt state).
    await orders_mod.on_buy(FC(f"ubuy:{pid}", FU(uid), msg), FBot(), FA, state, lang="fa")
    return pid, state


async def test_product_receipt_storage_failure_is_safe(db, monkeypatch, tmp_path) -> None:
    _pid, state = await _buy_and_arm_receipt(db, 9201)
    # Point storage at a path whose parent is a file → mkdir/write raises OSError,
    # which submit_receipt converts to ReceiptError(code="storage").
    afile = tmp_path / "not_a_dir"
    afile.write_text("x")
    monkeypatch.setattr(payment_service, "RECEIPTS_ROOT", afile / "receipts")

    msg = FM(FU(9201), photo=[FPhoto("f", 2048)])
    await orders_mod.on_receipt_in_state(msg, FBot(), FA, state, lang="fa")
    assert msg.answers == [FA("purchase.receipt.storage")]
    # Never stranded: still in the receipt-wait state so a resend just works.
    assert state.state == orders_mod.PurchaseStates.waiting_for_receipt


async def test_product_receipt_wrapper_catches_unexpected(db, monkeypatch) -> None:
    async def boom(*a, **k):
        raise RuntimeError("kaboom")
    monkeypatch.setattr(orders_mod, "_handle_receipt_inner", boom)
    msg = FM(FU(9202), photo=[FPhoto("f", 2048)])
    await orders_mod._handle_receipt(msg, FBot(), FA, FState(), "fa", None)
    assert msg.answers == [FA("purchase.receipt_failed")]


async def test_topup_receipt_storage_failure_is_safe(db, monkeypatch, tmp_path) -> None:
    async with db() as s:
        user = User(telegram_id=9203, first_name="B")
        s.add(user)
        await s.commit()
        topup = await wallet_service.create_topup_request(s, user.id, 50000)
        await s.commit()
        topup_id = topup.id
    afile = tmp_path / "not_a_dir2"
    afile.write_text("x")
    monkeypatch.setattr(payment_service, "RECEIPTS_ROOT", afile / "receipts")

    state = FState()
    await state.set_state(wallet_mod.WalletStates.waiting_for_topup_receipt)
    await state.update_data(topup_id=topup_id)
    msg = FM(FU(9203), photo=[FPhoto("f", 2048)])
    await wallet_mod.on_topup_receipt(msg, FBot(), FA, state, lang="fa")
    assert msg.answers == [FA("wallet.topup.receipt.storage")]


async def test_topup_receipt_wrapper_catches_unexpected(db, monkeypatch) -> None:
    async def boom(*a, **k):
        raise RuntimeError("kaboom")
    monkeypatch.setattr(wallet_mod, "_handle_topup_receipt", boom)
    state = FState()
    await state.set_state(wallet_mod.WalletStates.waiting_for_topup_receipt)
    msg = FM(FU(9204), photo=[FPhoto("f", 2048)])
    await wallet_mod.on_topup_receipt(msg, FBot(), FA, state, lang="fa")
    assert msg.answers == [FA("wallet.topup.receipt_rejected", error="internal")]


# ==========================================================================
# Bug 3 — card-to-card copy / paid / back buttons
# ==========================================================================
def test_build_copy_button_native_or_fallback() -> None:
    btn = build_copy_button("copy", "123", fallback_cb="paycpa")
    if copy_text_supported():
        assert getattr(btn, "copy_text", None) is not None
        assert btn.copy_text.text == "123"
    else:
        assert btn.callback_data == "paycpa"


def test_manual_receipt_keyboard_shape() -> None:
    kb = manual_receipt_keyboard(FA, amount=50000, card_number="6037-1",
                                 paid_cb="paydone", back_cb="upayback")
    rows = kb.inline_keyboard
    assert len(rows) == 3          # [copy amount, copy card], [paid], [back]
    assert len(rows[0]) == 2
    assert rows[1][0].callback_data == "paydone"
    assert rows[2][0].callback_data == "upayback"
    labels = [b.text for row in rows for b in row]
    assert FA("purchase.btn.copy_amount") in labels
    assert FA("purchase.btn.copy_card") in labels
    assert FA("purchase.btn.paid") in labels
    assert FA("btn.back") in labels


async def test_card_screen_has_copy_paid_back(db) -> None:
    _pid, state = await _buy_and_arm_receipt(db, 9301)
    # Drive the card screen directly to inspect its keyboard: copy amount + copy
    # card on the first row, «پرداخت کردم» and «بازگشت» below.
    msg = FM(FU(9301))
    await orders_mod._start_card_payment(msg, FU(9301), _pid, FA, state)
    kb = msg.markups[-1]
    assert kb is not None
    assert len(kb.inline_keyboard[0]) == 2
    assert kb.inline_keyboard[1][0].callback_data == "paydone"
    assert kb.inline_keyboard[2][0].callback_data == orders_mod.CB_PAY_BACK


async def test_copy_amount_fallback_echoes_value(db) -> None:
    state = FState()
    await state.update_data(pay_amount=50000)
    cb = FC("paycpa", FU(9302), FM(FU(9302)))
    await orders_mod.on_copy_amount(cb, FA, state)
    # The value is echoed in a copyable code block (never a fake "copied!").
    assert any("<code>50000</code>" in a for a in cb.message.answers)


async def test_pay_back_clears_state(db) -> None:
    state = FState()
    await state.set_state(orders_mod.PurchaseStates.waiting_for_receipt)
    cb = FC(orders_mod.CB_PAY_BACK, FU(9303), FM(FU(9303)))
    await orders_mod.on_pay_back(cb, FA, state, lang="fa")
    assert state.state is None
    assert cb.message.answers == [FA("purchase.card_cancelled")]


# ==========================================================================
# Bug 4-6 — admin dashboard / users / financial buttons respond
# ==========================================================================
async def test_admin_dashboard_button_responds(db) -> None:
    async with db() as s:
        s.add_all([User(telegram_id=1, first_name="a"),
                   User(telegram_id=2, first_name="b")])
        await s.commit()
    msg = FM(FU(1))
    await panel_mod.on_admin_dashboard(msg, FA, lang="fa", role=Role.OWNER)
    body = msg.answers[0]
    assert FA("admin.dash.title") in body
    assert FA("admin.dash.users", n=2) in body  # real user count rendered


async def test_admin_users_button_responds(db) -> None:
    async with db() as s:
        s.add(User(telegram_id=1, first_name="a", username="ada"))
        await s.commit()
    msg = FM(FU(1))
    await panel_mod.on_admin_users_button(msg, FA, lang="fa", role=Role.OWNER)
    body = msg.answers[0]
    assert FA("admin.users_panel.title") in body
    assert "@ada" in body


async def test_admin_financial_button_responds(db) -> None:
    msg = FM(FU(1))
    await panel_mod.on_admin_financial(msg, FA, lang="fa", role=Role.OWNER)
    body = msg.answers[0]
    assert FA("admin.fin.title") in body
    # A pending-receipts quick-jump button is attached.
    assert msg.markups[0] is not None


async def test_admin_buttons_denied_without_permission(db) -> None:
    for handler in (panel_mod.on_admin_dashboard,
                    panel_mod.on_admin_users_button,
                    panel_mod.on_admin_financial):
        msg = FM(FU(1))
        await handler(msg, FA, lang="fa", role=None)
        # No role → clear refusal, not a silent dead button.
        assert msg.answers == [FA("admin.not_authorized")]


# ==========================================================================
# Bug 7/8 — no dead menu buttons + safe fallback
# ==========================================================================
def test_no_admin_menu_button_is_dead() -> None:
    # Every label the admin reply-keyboard can show must map to a handler set.
    from app.bot.handlers.admin import products as p_mod
    from app.bot.handlers.admin import settings as set_mod
    from app.bot.handlers.admin import xui as x_mod
    from app.bot.handlers.admin.menu import USER_MENU_TEXTS
    from app.i18n import texts_for

    handled: set[str] = set()
    handled |= panel_mod.DASHBOARD_TEXTS
    handled |= panel_mod.USERS_TEXTS
    handled |= panel_mod.FINANCIAL_TEXTS
    handled |= texts_for("btn.admin.products")
    handled |= texts_for("btn.admin.servers")
    handled |= texts_for("btn.admin.settings")
    handled |= USER_MENU_TEXTS
    # Guard the two handler modules are importable (routes exist).
    assert p_mod.router and set_mod.router and x_mod.router

    for lang in ("fa", "en"):
        menu = admin_main_menu(lang)
        for row in menu.keyboard:
            for btn in row:
                assert btn.text in handled, f"dead admin button: {btn.text!r}"


async def test_admin_fallback_replies_only_to_admins(db) -> None:
    # An admin who sends unknown text gets a clear reply…
    admin_msg = FM(FU(1), text="یه چیز نامشخص")
    await fallback_mod.on_unknown_admin_text(admin_msg, FA, role=Role.OWNER)
    assert admin_msg.answers == [FA("admin.unknown_command")]

    # …an ordinary user is left untouched (falls through to other routers).
    user_msg = FM(FU(2), text="یه چیز نامشخص")
    await fallback_mod.on_unknown_admin_text(user_msg, FA, role=None)
    assert user_msg.answers == []


# ==========================================================================
# Bug 9 — /admin_debug (safe diagnostic, no secrets)
# ==========================================================================
async def test_admin_debug_reports_state_without_secrets(db) -> None:
    await _seed_methods(db, [
        dict(code="wallet", title="Wallet", method_type="wallet", sort_order=0),
        dict(code="card", title="Card", method_type="manual_receipt", sort_order=1),
    ])
    msg = FM(FU(1))
    state = FState()
    await panel_mod.on_admin_debug(msg, FA, state, lang="fa", role=Role.OWNER)
    body = msg.answers[0]
    assert "admin debug" in body
    assert "active payment methods: 2/2" in body
    assert "storage writable:" in body
    # Never leak secrets.
    for secret_word in ("token", "password", "secret", "api_token", "fernet"):
        assert secret_word not in body.lower()


async def test_admin_debug_denied_without_role(db) -> None:
    msg = FM(FU(1))
    await panel_mod.on_admin_debug(msg, FA, FState(), lang="fa", role=None)
    assert msg.answers == [FA("admin.not_authorized")]

"""Receipt storage hardening (Part A): atomic writes, auto-created dirs, safe
errors, and the shared STORAGE_ROOT config the bot + backend both resolve."""
from __future__ import annotations

from pathlib import Path
from typing import Any

import pytest
import pytest_asyncio
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker, create_async_engine
from sqlalchemy.pool import StaticPool

import app.bot.handlers.user.orders as orders_mod
import app.bot.notifications as notify_mod
from app.models import Base, Product
from app.services import order_service, payment_service, user_service

RECEIPT_BYTES = b"\x89PNG\r\n\x1a\n" + b"receipt-content"


# --------------------------------------------------------------------------
# Bot fakes (receipt upload handler)
# --------------------------------------------------------------------------
class FU:
    def __init__(self, uid):
        self.id = uid
        self.username = "buyer"
        self.first_name = "B"
        self.last_name = "Uyer"


class FM:
    def __init__(self, from_user=None, photo=None, document=None, text=""):
        self.from_user = from_user
        self.photo = photo
        self.document = document
        self.text = text
        self.answers: list[str] = []

    async def answer(self, text: str, **kwargs: Any) -> None:
        self.answers.append(text)


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
    async def get_file(self, *a, **k):
        return None

    async def download_file(self, *a, **k):
        return None


@pytest_asyncio.fixture
async def db(monkeypatch, tmp_path):
    engine = create_async_engine("sqlite+aiosqlite://", poolclass=StaticPool,
                                 connect_args={"check_same_thread": False})
    async with engine.begin() as conn:
        await conn.run_sync(Base.metadata.create_all)
    maker = async_sessionmaker(engine, class_=AsyncSession, expire_on_commit=False)
    monkeypatch.setattr(orders_mod, "SessionLocal", maker)
    monkeypatch.setattr(notify_mod, "SessionLocal", maker)
    monkeypatch.setattr(payment_service, "RECEIPTS_ROOT", tmp_path / "receipts")

    async def fake_download(bot, file_id):
        return RECEIPT_BYTES
    monkeypatch.setattr(orders_mod, "_download_telegram_file", fake_download)
    try:
        yield maker
    finally:
        await engine.dispose()


async def _order_with_payment(maker, uid: int):
    async with maker() as s:
        user, _ = await user_service.create_or_update_from_telegram(
            s, telegram_id=uid, username="b", first_name="B", last_name="U")
        await s.commit()
        prod = Product(type="license", title="Key", price=10000, is_active=True, is_hidden=False)
        s.add(prod)
        await s.commit()
        order = await order_service.create_order(s, user.id, prod.id)
        await s.commit()
        await payment_service.create_payment_for_order(s, order)
        await s.commit()
        return user.id, order.id


def _rf(name: str, mime: str) -> payment_service.ReceiptFile:
    return payment_service.ReceiptFile(
        content=RECEIPT_BYTES, original_name=name, mime_type=mime, file_id="FID")


# ==========================================================================
# save_receipt_bytes (atomic write helper)
# ==========================================================================
def test_save_receipt_bytes_atomic_creates_dirs(tmp_path) -> None:
    dest = tmp_path / "receipts" / "2026" / "07" / "DC-1_receipt.jpg"
    payment_service.save_receipt_bytes(dest, RECEIPT_BYTES, file_id="X", mime_type="image/jpeg")
    assert dest.exists() and dest.read_bytes() == RECEIPT_BYTES
    # No temp file is left behind.
    leftovers = list(dest.parent.glob(".*tmp*"))
    assert leftovers == []


def test_save_receipt_bytes_storage_error(tmp_path) -> None:
    # Parent path is a regular file → mkdir/write raises OSError → ReceiptError.
    afile = tmp_path / "afile"
    afile.write_text("x")
    dest = afile / "receipts" / "r.jpg"
    with pytest.raises(payment_service.ReceiptError) as ei:
        payment_service.save_receipt_bytes(dest, RECEIPT_BYTES)
    assert ei.value.code == "storage"


# ==========================================================================
# submit_receipt: photo / pdf / image-document all store a path + metadata
# ==========================================================================
async def _submit(maker, uid, order_id, rf) -> Any:
    async with maker() as s:
        payment = await payment_service.submit_receipt(s, order_id, uid, rf)
        await s.commit()
        return payment


async def test_submit_receipt_photo_stores_path(db) -> None:
    uid, oid = await _order_with_payment(db, 5101)
    payment = await _submit(db, uid, oid, _rf("receipt.jpg", "image/jpeg"))
    assert payment.receipt_path and payment.receipt_path.endswith("_receipt.jpg")
    assert payment.receipt_file_id == "FID"
    assert payment.receipt_mime_type == "image/jpeg"
    assert payment.submitted_at is not None
    assert payment.status == "receipt_submitted"  # pending review (not approved)
    stored = payment_service.resolve_receipt_path(payment.receipt_path)
    assert stored is not None and stored.exists()


async def test_submit_receipt_pdf_stores_path(db) -> None:
    uid, oid = await _order_with_payment(db, 5102)
    payment = await _submit(db, uid, oid, _rf("proof.pdf", "application/pdf"))
    assert payment.receipt_path.endswith("_proof.pdf")
    assert payment_service.resolve_receipt_path(payment.receipt_path).exists()


async def test_submit_receipt_image_document_stores_path(db) -> None:
    uid, oid = await _order_with_payment(db, 5103)
    payment = await _submit(db, uid, oid, _rf("scan.png", "image/png"))
    assert payment.receipt_path.endswith("_scan.png")
    assert payment_service.resolve_receipt_path(payment.receipt_path).exists()


async def test_missing_storage_dir_is_autocreated(db, monkeypatch, tmp_path) -> None:
    # Point storage at a directory tree that does not exist yet.
    fresh = tmp_path / "brand" / "new" / "receipts"
    monkeypatch.setattr(payment_service, "RECEIPTS_ROOT", fresh)
    uid, oid = await _order_with_payment(db, 5104)
    payment = await _submit(db, uid, oid, _rf("r.jpg", "image/jpeg"))
    assert (fresh / payment.receipt_path).exists()


async def test_submit_receipt_storage_failure_raises(db, monkeypatch, tmp_path) -> None:
    afile = tmp_path / "not_a_dir"
    afile.write_text("x")
    monkeypatch.setattr(payment_service, "RECEIPTS_ROOT", afile / "receipts")
    uid, oid = await _order_with_payment(db, 5105)
    with pytest.raises(payment_service.ReceiptError) as ei:
        await _submit(db, uid, oid, _rf("r.jpg", "image/jpeg"))
    assert ei.value.code == "storage"


# ==========================================================================
# Handler: success clears state + notifies admin; download failure keeps state
# ==========================================================================
async def test_receipt_handler_success_clears_state_and_notifies(db, monkeypatch) -> None:
    uid, oid = await _order_with_payment(db, 5201)
    called: list[bool] = []

    async def fake_notify(*a, **k):
        called.append(True)
    monkeypatch.setattr(notify_mod, "notify_receipt_submitted", fake_notify)

    state = FState()
    await state.set_state(orders_mod.PurchaseStates.waiting_for_receipt)
    await state.update_data(order_id=oid)
    msg = FM(FU(5201), photo=[FPhoto("FID", 2048)])
    await orders_mod.on_receipt_in_state(msg, FBot(), lambda k, **p: k, state, lang="fa")

    assert state.state is None                       # FSM cleared on success
    assert called == [True]                          # admin notified
    async with db() as s:
        payment = await payment_service.get_payment_by_order(s, oid)
    assert payment.status == "receipt_submitted" and payment.receipt_path


async def test_receipt_handler_download_failure_keeps_state(db, monkeypatch) -> None:
    uid, oid = await _order_with_payment(db, 5202)

    async def boom(bot, file_id):
        raise RuntimeError("telegram down")
    monkeypatch.setattr(orders_mod, "_download_telegram_file", boom)

    state = FState()
    await state.set_state(orders_mod.PurchaseStates.waiting_for_receipt)
    await state.update_data(order_id=oid)
    msg = FM(FU(5202), photo=[FPhoto("FID", 2048)])
    await orders_mod.on_receipt_in_state(msg, FBot(), lambda k, **p: k, state, lang="fa")

    assert msg.answers == ["purchase.download_failed"]
    assert state.state == orders_mod.PurchaseStates.waiting_for_receipt  # not stranded


# ==========================================================================
# Shared storage config (bot + backend resolve the same root)
# ==========================================================================
def test_storage_root_honours_env(monkeypatch) -> None:
    monkeypatch.setenv("STORAGE_ROOT", "/mnt/shared/storage")
    assert payment_service._storage_root() == Path("/mnt/shared/storage")
    monkeypatch.delenv("STORAGE_ROOT", raising=False)
    # Default falls back to the repo storage/ dir (ends with /storage).
    assert payment_service._storage_root().name == "storage"


def test_wallet_receipts_share_payment_storage_root() -> None:
    # wallet_service writes under the SAME RECEIPTS_ROOT the backend serves from.
    from app.services import wallet_service
    assert wallet_service.payment_service.RECEIPTS_ROOT == payment_service.RECEIPTS_ROOT

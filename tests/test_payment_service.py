"""payment_service: payment creation, receipt validation + safe storage, audit."""
from __future__ import annotations

from pathlib import Path

import pytest
from sqlalchemy import select

from app.models import AuditLog, Product, User
from app.services import order_service, payment_service
from app.services.payment_service import ReceiptError, ReceiptFile


async def _seed_order(db_session):
    u = User(telegram_id=200, first_name="Buyer")
    db_session.add(u)
    p = Product(type="license", title="Key", price=90_000, is_active=True, is_hidden=False)
    db_session.add(p)
    await db_session.flush()
    order = await order_service.create_order(db_session, u.id, p.id)
    payment = await payment_service.create_payment_for_order(db_session, order)
    await db_session.commit()
    return u, order, payment


def _png(n: int = 100) -> bytes:
    return b"\x89PNG\r\n\x1a\n" + b"0" * n


async def test_create_payment_for_order(db_session) -> None:
    _u, order, payment = await _seed_order(db_session)
    assert payment.status == "pending"
    assert payment.amount == order.final_amount
    assert payment.method == "card_to_card"


async def test_submit_receipt_happy_path(db_session, tmp_path, monkeypatch) -> None:
    monkeypatch.setattr(payment_service, "RECEIPTS_ROOT", tmp_path)
    u, order, _payment = await _seed_order(db_session)
    fi = ReceiptFile(content=_png(), original_name="receipt.png",
                     mime_type="image/png", file_id="tg1")
    payment = await payment_service.submit_receipt(db_session, order.id, u.id, fi)
    await db_session.commit()

    assert payment.status == "receipt_submitted"
    assert payment.submitted_at is not None
    assert payment.receipt_file_id == "tg1"
    # Order advanced to the review queue.
    refreshed = await order_service.get_order(db_session, order.id)
    assert refreshed.status == "waiting_admin"
    # File actually written under YYYY/MM/<order>_<name>.
    assert payment.receipt_path.startswith(f"{tmp_path.name}") is False  # relative, not absolute
    stored = tmp_path / payment.receipt_path
    assert stored.is_file() and stored.read_bytes() == fi.content
    assert order.order_number in payment.receipt_path


async def test_reject_unsafe_extension(db_session, tmp_path, monkeypatch) -> None:
    monkeypatch.setattr(payment_service, "RECEIPTS_ROOT", tmp_path)
    u, order, _payment = await _seed_order(db_session)
    fi = ReceiptFile(content=b"MZ...", original_name="virus.exe", mime_type="application/x-msdownload")
    with pytest.raises(ReceiptError) as ei:
        await payment_service.submit_receipt(db_session, order.id, u.id, fi)
    assert ei.value.code == "unsupported_type"


async def test_reject_oversized_receipt(db_session, tmp_path, monkeypatch) -> None:
    monkeypatch.setattr(payment_service, "RECEIPTS_ROOT", tmp_path)
    u, order, _payment = await _seed_order(db_session)
    big = b"0" * (payment_service.MAX_RECEIPT_BYTES + 1)
    fi = ReceiptFile(content=big, original_name="huge.png", mime_type="image/png")
    with pytest.raises(ReceiptError) as ei:
        await payment_service.submit_receipt(db_session, order.id, u.id, fi)
    assert ei.value.code == "too_large"


async def test_reject_receipt_from_other_user(db_session, tmp_path, monkeypatch) -> None:
    monkeypatch.setattr(payment_service, "RECEIPTS_ROOT", tmp_path)
    _u, order, _payment = await _seed_order(db_session)
    fi = ReceiptFile(content=_png(), original_name="r.png", mime_type="image/png")
    with pytest.raises(ReceiptError) as ei:
        await payment_service.submit_receipt(db_session, order.id, 987654, fi)
    assert ei.value.code == "not_your_order"


async def test_cannot_submit_twice(db_session, tmp_path, monkeypatch) -> None:
    monkeypatch.setattr(payment_service, "RECEIPTS_ROOT", tmp_path)
    u, order, _payment = await _seed_order(db_session)
    fi = ReceiptFile(content=_png(), original_name="r.png", mime_type="image/png")
    await payment_service.submit_receipt(db_session, order.id, u.id, fi)
    await db_session.commit()
    with pytest.raises(ReceiptError) as ei:
        await payment_service.submit_receipt(db_session, order.id, u.id, fi)
    assert ei.value.code in ("already_submitted", "order_not_receivable")


async def test_receipt_submission_writes_audit(db_session, tmp_path, monkeypatch) -> None:
    monkeypatch.setattr(payment_service, "RECEIPTS_ROOT", tmp_path)
    u, order, payment = await _seed_order(db_session)
    fi = ReceiptFile(content=_png(), original_name="r.png", mime_type="image/png")
    await payment_service.submit_receipt(db_session, order.id, u.id, fi)
    await db_session.commit()
    rows = (await db_session.execute(select(AuditLog))).scalars().all()
    actions = [r.action for r in rows]
    assert "receipt_submitted" in actions
    # No secret bytes leak into audit metadata.
    blob = " ".join(f"{r.new_value} {r.meta}" for r in rows)
    assert "PNG" not in blob


def test_path_traversal_is_blocked(tmp_path, monkeypatch) -> None:
    monkeypatch.setattr(payment_service, "RECEIPTS_ROOT", tmp_path)
    # A malicious stored value must not resolve outside the receipts root.
    assert payment_service.resolve_receipt_path("../../etc/passwd") is None
    assert payment_service.resolve_receipt_path("/etc/passwd") is None
    # A legitimate but missing file resolves to None (not an error).
    assert payment_service.resolve_receipt_path("2026/07/x.png") is None


def test_sanitized_filename_has_no_separators() -> None:
    rel = payment_service.build_receipt_relpath(
        "DC-1", "../../evil name.png", "image/png",
        __import__("datetime").datetime(2026, 7, 5),
    )
    assert ".." not in rel
    assert rel.startswith("2026/07/")
    assert Path(rel).name.count("/") == 0

"""Payments + receipt handling (card-to-card, Phase 3).

`submit_receipt` validates an uploaded file (type + size), writes it under
``storage/receipts/YYYY/MM/<order_number>_<safe_name>``, records only its
metadata + a path *relative to the receipts root*, then flips the payment to
receipt_submitted and the order to waiting_admin. Approval/rejection are NOT
here.

Receipt bytes never touch the DB or the audit log; filenames are sanitised and
the serving layer re-validates containment to defeat path traversal.
"""
from __future__ import annotations

import re
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.models.payment import Payment
from app.services import audit_service, order_service

# storage/receipts/ at the repo root. Overridable in tests via monkeypatch.
RECEIPTS_ROOT: Path = Path(__file__).resolve().parents[2] / "storage" / "receipts"

MAX_RECEIPT_BYTES = 10 * 1024 * 1024  # 10 MB
ALLOWED_EXTENSIONS: frozenset[str] = frozenset({"jpg", "jpeg", "png", "webp", "pdf"})
_EXT_FOR_MIME = {
    "image/jpeg": "jpg",
    "image/png": "png",
    "image/webp": "webp",
    "application/pdf": "pdf",
}
_MIME_FOR_EXT = {
    "jpg": "image/jpeg",
    "jpeg": "image/jpeg",
    "png": "image/png",
    "webp": "image/webp",
    "pdf": "application/pdf",
}


class ReceiptError(ValueError):
    """A user-facing reason a receipt was rejected."""

    code = "receipt_error"

    def __init__(self, message: str, code: str | None = None) -> None:
        super().__init__(message)
        if code is not None:
            self.code = code


@dataclass
class ReceiptFile:
    """A downloaded Telegram receipt handed to the service for storage."""

    content: bytes
    original_name: str
    mime_type: str | None = None
    file_id: str | None = None

    @property
    def size(self) -> int:
        return len(self.content)


def _now() -> datetime:
    return datetime.now(timezone.utc)


def _ext_of(original_name: str, mime_type: str | None) -> str:
    ext = Path(original_name or "").suffix.lower().lstrip(".")
    if not ext and mime_type:
        ext = _EXT_FOR_MIME.get(mime_type.split(";")[0].strip().lower(), "")
    return ext


def precheck_receipt(original_name: str, size: int | None, mime_type: str | None = None) -> None:
    """Cheap pre-download guard: reject bad types always and known-oversize files.

    `size` may be None/0 when Telegram doesn't report it; the authoritative size
    check happens in `validate_receipt` once the bytes are in hand.
    """
    ext = _ext_of(original_name, mime_type)
    if ext not in ALLOWED_EXTENSIONS:
        raise ReceiptError("unsupported file type", code="unsupported_type")
    if size and size > MAX_RECEIPT_BYTES:
        raise ReceiptError("the file is too large", code="too_large")


def validate_receipt(original_name: str, size: int, mime_type: str | None = None) -> str:
    """Validate type + size; return the accepted extension. Raises ReceiptError."""
    if size <= 0:
        raise ReceiptError("the file is empty", code="empty")
    if size > MAX_RECEIPT_BYTES:
        raise ReceiptError("the file is too large", code="too_large")
    ext = _ext_of(original_name, mime_type)
    if ext not in ALLOWED_EXTENSIONS:
        raise ReceiptError("unsupported file type", code="unsupported_type")
    return ext


def _sanitize_filename(name: str, *, fallback_ext: str) -> str:
    """A safe basename: strip directories, keep [A-Za-z0-9._-], ensure an ext."""
    base = Path(name or "").name  # drops any directory components
    base = re.sub(r"[^A-Za-z0-9._-]", "_", base).strip("._") or "receipt"
    if not Path(base).suffix:
        base = f"{base}.{fallback_ext}"
    return base[:120]


def build_receipt_relpath(
    order_number: str, original_name: str, mime_type: str | None, when: datetime
) -> str:
    """Relative path under RECEIPTS_ROOT: ``YYYY/MM/<order_number>_<safe_name>``."""
    ext = _ext_of(original_name, mime_type) or "bin"
    safe_name = _sanitize_filename(original_name, fallback_ext=ext)
    safe_order = re.sub(r"[^A-Za-z0-9._-]", "_", order_number or "order")
    return f"{when.year:04d}/{when.month:02d}/{safe_order}_{safe_name}"


def resolve_receipt_path(stored_rel: str | None) -> Path | None:
    """Resolve a stored relative path to an absolute file, guarding traversal.

    Returns None if the value is empty, escapes the receipts root, or is missing.
    """
    if not stored_rel:
        return None
    root = RECEIPTS_ROOT.resolve()
    candidate = (root / stored_rel).resolve()
    try:
        candidate.relative_to(root)
    except ValueError:
        return None  # path traversal attempt
    if not candidate.is_file():
        return None
    return candidate


async def create_payment_for_order(session: AsyncSession, order) -> Payment:
    """Create the pending card-to-card Payment that tracks an order's money side."""
    payment = Payment(
        order_id=order.id,
        user_id=order.user_id,
        amount=order.final_amount,
        method=order.payment_method,
        status="pending",
    )
    session.add(payment)
    await session.flush()
    await audit_service.log(
        session, actor_type="user", actor_id=order.user_id, action="payment_created",
        target_type="payment", target_id=payment.id,
        new=f"order={order.order_number} amount={order.final_amount}",
    )
    await session.refresh(payment)
    return payment


async def get_payment_by_order(session: AsyncSession, order_id: int) -> Payment | None:
    stmt = (
        select(Payment)
        .where(Payment.order_id == order_id)
        .order_by(Payment.id.desc())
        .limit(1)
    )
    return await session.scalar(stmt)


async def list_receipt_submitted_payments(
    session: AsyncSession, *, limit: int = 50, offset: int = 0
) -> list[Payment]:
    stmt = (
        select(Payment)
        .where(Payment.status == "receipt_submitted")
        .order_by(Payment.id.desc())
        .limit(limit)
        .offset(offset)
    )
    return list((await session.execute(stmt)).scalars().all())


async def submit_receipt(
    session: AsyncSession, order_id: int, user_id: int, file_info: ReceiptFile
) -> Payment:
    """Store a receipt and advance order -> waiting_admin, payment -> submitted.

    Enforces ownership, that the order is still pending_payment and the payment
    still pending, and validates the file. Raises ReceiptError on any failure.
    """
    order = await order_service.get_order(session, order_id)
    if order is None:
        raise ReceiptError("order not found", code="order_not_found")
    if order.user_id != user_id:
        raise ReceiptError("not your order", code="not_your_order")
    try:
        order_service.ensure_order_can_receive_receipt(order)
    except order_service.OrderError as exc:  # normalise to ReceiptError for callers
        raise ReceiptError(str(exc), code=exc.code) from exc

    payment = await get_payment_by_order(session, order_id)
    if payment is None:
        raise ReceiptError("no payment to attach the receipt to", code="no_payment")
    if payment.status != "pending":
        raise ReceiptError("a receipt was already submitted", code="already_submitted")

    ext = validate_receipt(file_info.original_name, file_info.size, file_info.mime_type)

    when = _now()
    rel = build_receipt_relpath(order.order_number, file_info.original_name, file_info.mime_type, when)
    dest = RECEIPTS_ROOT / rel
    dest.parent.mkdir(parents=True, exist_ok=True)
    dest.write_bytes(file_info.content)

    payment.receipt_path = rel
    payment.receipt_file_id = file_info.file_id
    payment.receipt_mime_type = (file_info.mime_type or _MIME_FOR_EXT.get(ext))
    payment.receipt_original_name = file_info.original_name
    payment.receipt_size = file_info.size
    payment.status = "receipt_submitted"
    payment.submitted_at = when

    order.status = "waiting_admin"

    await audit_service.log(
        session, actor_type="user", actor_id=user_id, action="receipt_submitted",
        target_type="payment", target_id=payment.id,
        meta=f"order={order.order_number} size={file_info.size} mime={payment.receipt_mime_type}",
    )
    await session.refresh(payment)
    return payment

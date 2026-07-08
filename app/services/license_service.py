"""License stock + real license delivery (Phase 5).

Manages the `license_items` pool: import (parsed text / CSV / file), listing and
counts, and — the critical part — reserving and selling exactly one license per
approved order, atomically, so a license is never sold twice. Delivery sends the
credentials to the buyer in Telegram; a send failure leaves the license reserved
and the order flagged so an admin can redeliver.

Passwords are stored (delivery needs them) but never appear in audit metadata.
"""
from __future__ import annotations

import logging
import re
from datetime import datetime, timezone

from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.models.license_item import LicenseItem
from app.models.order import Order
from app.models.product import Product
from app.services import audit_service, order_service

log = logging.getLogger("license")


class LicenseError(ValueError):
    code = "license_error"

    def __init__(self, message: str, code: str | None = None) -> None:
        super().__init__(message)
        if code is not None:
            self.code = code


class NoLicenseAvailableError(LicenseError):
    def __init__(self, message: str = "no license available") -> None:
        super().__init__(message, code="no_license_available")


def _now() -> datetime:
    return datetime.now(timezone.utc)


def mask_email(email: str) -> str:
    """`alice@example.com` -> `a***@example.com` for safe audit metadata."""
    name, _, domain = (email or "").partition("@")
    if not domain:
        return "***"
    head = name[:1] or "*"
    return f"{head}***@{domain}"


# --------------------------------------------------------------------------
# Parser
# --------------------------------------------------------------------------
_KV_RE = re.compile(r"(?i)^(email|password|note)\s*:\s*(.*)$")


def parse_license_text(raw_text: str) -> tuple[list[dict], list[dict]]:
    """Parse ``EMAIL:/PASSWORD:/NOTE:`` blocks (or ``email,password,note`` CSV lines).

    Returns (items, errors). Each item is {email, password, note, block}; each
    error is {block, error, raw}. Blank lines separate blocks; keys are
    case-insensitive; email + password are required, note optional.
    """
    items: list[dict] = []
    errors: list[dict] = []
    blocks = re.split(r"\n\s*\n", (raw_text or "").strip())
    block_no = 0
    for raw_block in blocks:
        block = raw_block.strip()
        if not block:
            continue
        block_no += 1
        lines = [ln.strip() for ln in block.splitlines() if ln.strip()]
        is_kv = any(_KV_RE.match(ln) for ln in lines)
        if is_kv:
            kv: dict[str, str] = {}
            unknown: list[str] = []
            for ln in lines:
                m = _KV_RE.match(ln)
                if m:
                    kv[m.group(1).lower()] = m.group(2).strip()
                else:
                    unknown.append(ln)  # a stray line inside an EMAIL/PASSWORD block
            if unknown:
                # Never silently drop lines — a typo'd key or pasted junk (e.g.
                # `EMIAL: a@x.com`) would otherwise vanish and import bad data.
                errors.append({"block": block_no,
                               "error": "unrecognized line(s): " + "; ".join(unknown),
                               "raw": block})
                continue
            email, password = kv.get("email", ""), kv.get("password", "")
            note = kv.get("note") or None
            if not email or not password:
                errors.append({"block": block_no, "error": "email and password are required",
                               "raw": block})
                continue
            items.append({"email": email, "password": password, "note": note, "block": block_no})
        else:
            # CSV: one item per line.
            for ln in lines:
                parts = [p.strip() for p in ln.split(",")]
                if len(parts) < 2 or not parts[0] or not parts[1]:
                    errors.append({"block": block_no, "error": "malformed line", "raw": ln})
                    continue
                note = parts[2] if len(parts) > 2 and parts[2] else None
                items.append({"email": parts[0], "password": parts[1], "note": note,
                              "block": block_no})
    return items, errors


# --------------------------------------------------------------------------
# Import / add
# --------------------------------------------------------------------------
async def _product_or_raise(session: AsyncSession, product_id: int) -> Product:
    product = await session.get(Product, product_id)
    if product is None:
        raise LicenseError("product not found", code="product_not_found")
    if product.type != "license":
        raise LicenseError("licenses can only be imported for license products",
                           code="not_license_product")
    return product


async def _existing_emails(session: AsyncSession, product_id: int) -> set[str]:
    rows = (await session.execute(
        select(LicenseItem.email).where(LicenseItem.product_id == product_id)
    )).scalars().all()
    return {e.lower() for e in rows}


async def add_license(
    session: AsyncSession, product_id: int, email: str, password: str,
    note: str | None = None, admin_id: int | None = None,
) -> LicenseItem:
    await _product_or_raise(session, product_id)
    email = (email or "").strip()
    password = (password or "").strip()
    if not email or not password:
        raise LicenseError("email and password are required", code="missing_fields")
    if email.lower() in await _existing_emails(session, product_id):
        raise LicenseError("a license with this email already exists", code="duplicate")
    lic = LicenseItem(product_id=product_id, email=email, password=password,
                      note=(note or None), status="available", imported_by_admin_id=admin_id)
    session.add(lic)
    await session.flush()
    await audit_service.log(
        session, actor_type="admin", actor_id=admin_id, action="license_added",
        target_type="license", target_id=lic.id,
        meta=f"product_id={product_id} email={mask_email(email)}",  # never the password
    )
    await session.refresh(lic)
    return lic


async def bulk_import_licenses(
    session: AsyncSession, product_id: int, raw_text: str, admin_id: int | None = None,
) -> dict:
    """Parse + insert licenses; report imported / duplicate / invalid counts."""
    await _product_or_raise(session, product_id)
    items, errors = parse_license_text(raw_text)

    existing = await _existing_emails(session, product_id)
    seen: set[str] = set()
    imported = dup_in_file = dup_in_db = 0
    for it in items:
        key = it["email"].lower()
        if key in seen:
            dup_in_file += 1
            continue
        seen.add(key)
        if key in existing:
            dup_in_db += 1
            continue
        session.add(LicenseItem(
            product_id=product_id, email=it["email"], password=it["password"],
            note=it["note"], status="available", imported_by_admin_id=admin_id,
        ))
        existing.add(key)
        imported += 1
    if imported:
        await session.flush()

    result = {"imported": imported, "duplicates_in_file": dup_in_file,
              "duplicates_in_db": dup_in_db, "invalid": len(errors),
              "errors": errors, "parsed": len(items)}
    if imported:
        await audit_service.log(
            session, actor_type="admin", actor_id=admin_id, action="license_bulk_imported",
            target_type="product", target_id=product_id,
            meta=f"imported={imported} dup_file={dup_in_file} dup_db={dup_in_db} invalid={len(errors)}",
        )
    elif errors:
        await audit_service.log(
            session, actor_type="admin", actor_id=admin_id, action="license_import_failed",
            target_type="product", target_id=product_id, meta=f"invalid={len(errors)}",
        )
    return result


async def import_licenses_from_file(
    session: AsyncSession, product_id: int, file_path: str, admin_id: int | None = None,
) -> dict:
    """Import from a .txt/.csv file (XLSX is intentionally out of scope for now)."""
    from pathlib import Path
    path = Path(file_path)
    if not path.is_file():
        raise LicenseError("file not found", code="file_not_found")
    if path.suffix.lower() not in (".txt", ".csv"):
        raise LicenseError("only .txt and .csv are supported", code="unsupported_file")
    raw = path.read_text(encoding="utf-8", errors="replace")
    return await bulk_import_licenses(session, product_id, raw, admin_id=admin_id)


# --------------------------------------------------------------------------
# Queries
# --------------------------------------------------------------------------
async def get_license(session: AsyncSession, license_id: int) -> LicenseItem | None:
    return await session.get(LicenseItem, license_id)


async def get_license_by_order(session: AsyncSession, order_id: int) -> LicenseItem | None:
    """The active (reserved/sold) license attached to an order, if any."""
    stmt = (
        select(LicenseItem)
        .where(LicenseItem.order_id == order_id,
               LicenseItem.status.in_(("reserved", "sold")))
        .order_by(LicenseItem.id.desc())
        .limit(1)
    )
    return await session.scalar(stmt)


async def list_licenses(
    session: AsyncSession, *, product_id: int | None = None, status: str | None = None,
    limit: int = 50, offset: int = 0,
) -> list[LicenseItem]:
    stmt = select(LicenseItem)
    if product_id is not None:
        stmt = stmt.where(LicenseItem.product_id == product_id)
    if status:
        stmt = stmt.where(LicenseItem.status == status)
    stmt = stmt.order_by(LicenseItem.id.desc()).limit(limit).offset(offset)
    return list((await session.execute(stmt)).scalars().all())


async def list_sold(session: AsyncSession, *, limit: int = 100, offset: int = 0) -> list[LicenseItem]:
    stmt = (select(LicenseItem).where(LicenseItem.status == "sold")
            .order_by(LicenseItem.sold_at.desc().nullslast(), LicenseItem.id.desc())
            .limit(limit).offset(offset))
    return list((await session.execute(stmt)).scalars().all())


async def list_user_licenses(
    session: AsyncSession, user_id: int, *, limit: int = 50, offset: int = 0
) -> list[LicenseItem]:
    stmt = (select(LicenseItem)
            .where(LicenseItem.sold_to_user_id == user_id, LicenseItem.status == "sold")
            .order_by(LicenseItem.sold_at.desc().nullslast(), LicenseItem.id.desc())
            .limit(limit).offset(offset))
    return list((await session.execute(stmt)).scalars().all())


async def count_user_licenses(session: AsyncSession, user_id: int) -> int:
    return int(await session.scalar(
        select(func.count(LicenseItem.id)).where(
            LicenseItem.sold_to_user_id == user_id, LicenseItem.status == "sold"
        )
    ) or 0)


async def count_available(session: AsyncSession, product_id: int) -> int:
    return int(await session.scalar(
        select(func.count(LicenseItem.id)).where(
            LicenseItem.product_id == product_id, LicenseItem.status == "available"
        )
    ) or 0)


async def count_by_status(session: AsyncSession, product_id: int) -> dict[str, int]:
    rows = await session.execute(
        select(LicenseItem.status, func.count(LicenseItem.id))
        .where(LicenseItem.product_id == product_id)
        .group_by(LicenseItem.status)
    )
    return {status: count for status, count in rows.all()}


async def low_stock_products(session: AsyncSession, threshold: int) -> list[dict]:
    """License products whose available stock is below the threshold."""
    products = (await session.execute(
        select(Product).where(Product.type == "license").order_by(Product.id)
    )).scalars().all()
    out = []
    for p in products:
        avail = await count_available(session, p.id)
        if avail < threshold:
            out.append({"product": p, "available": avail, "threshold": threshold})
    return out


# --------------------------------------------------------------------------
# Reservation + delivery
# --------------------------------------------------------------------------
async def reserve_available_license(
    session: AsyncSession, product_id: int, order_id: int, user_id: int
) -> LicenseItem:
    """Atomically claim one available license (row-locked on Postgres)."""
    stmt = (
        select(LicenseItem)
        .where(LicenseItem.product_id == product_id, LicenseItem.status == "available")
        .order_by(LicenseItem.id)
        .limit(1)
        .with_for_update(skip_locked=True)  # ignored on SQLite; real lock on Postgres
    )
    lic = await session.scalar(stmt)
    if lic is None:
        raise NoLicenseAvailableError()
    lic.status = "reserved"
    lic.order_id = order_id
    lic.sold_to_user_id = user_id
    lic.reserved_at = _now()
    await session.flush()
    await audit_service.log(
        session, actor_type="system", actor_id=None, action="license_reserved",
        target_type="license", target_id=lic.id,
        meta=f"order_id={order_id} user_id={user_id}",
    )
    return lic


def build_delivery_message(order, product, lic: LicenseItem, lang: str = "fa") -> str:
    from app.i18n import t
    number = order.order_number if order is not None else "—"
    lines = [
        t("license.delivery.title", lang),
        "",
        t("license.delivery.order", lang, number=number),
        t("license.delivery.product", lang, title=product.title if product else "—"),
        "",
        t("license.delivery.email_label", lang),
        f"<code>{lic.email}</code>",
        "",
        t("license.delivery.password_label", lang),
        f"<code>{lic.password}</code>",
    ]
    if lic.note:
        lines += ["", t("license.delivery.note_label", lang), lic.note]
    lines += ["", t("license.delivery.keep_safe", lang)]
    return "\n".join(lines)


async def _deliver_to_user(bot, order, product, lic: LicenseItem, lang: str = "fa") -> bool:
    """Send the license to the buyer in Telegram. Never raises; returns success."""
    user = order.user
    target = user.telegram_id if user else None
    if not target:
        return False
    text = build_delivery_message(order, product, lic, lang)
    own_bot = None
    b = bot
    if b is None:
        from app.config import settings
        if not settings.TELEGRAM_BOT_TOKEN:
            return False
        from aiogram import Bot
        own_bot = Bot(settings.TELEGRAM_BOT_TOKEN)
        b = own_bot
    try:
        await b.send_message(target, text, parse_mode="HTML")
        return True
    except Exception as exc:  # noqa: BLE001 - a send failure is a soft failure
        log.warning("License delivery send failed for order %s: %s", order.order_number, exc)
        return False
    finally:
        if own_bot is not None:
            try:
                await own_bot.session.close()
            except Exception:  # noqa: BLE001
                pass


async def deliver_license_for_order(
    session: AsyncSession, order_id: int, *, bot=None, actor_id: int | None = None,
) -> dict:
    """Reserve + sell one license and deliver it. Idempotent per order.

    The order row is locked FOR UPDATE and held for the WHOLE operation — there
    is no intermediate commit — so two concurrent deliveries for the SAME order
    serialize: the loser blocks, then sees status=delivered (or the already-
    reserved license) and returns instead of reserving/selling a *second*
    license. The lock is a no-op on SQLite (no row locks; single-writer anyway).

    The reservation is inlined here rather than delegated to
    ``reserve_available_license`` precisely because that helper writes an audit
    row (which commits internally), and a mid-operation commit would release the
    order lock and re-open the double-delivery race. All audit rows are therefore
    written at the terminal points only.
    """
    # Serialize concurrent same-order deliveries on the order row (held until we
    # return, i.e. until the terminal audit commit — no commit before then).
    await session.execute(
        select(Order.id).where(Order.id == order_id).with_for_update()
    )
    order = await order_service.get_order(session, order_id)
    if order is None:
        raise LicenseError("order not found", code="order_not_found")
    product = order.product
    if product is None or product.type != "license":
        raise LicenseError("not a license order", code="not_license_order")

    # Already delivered → do not sell another; return the existing result.
    if order.status == "delivered":
        lic = await get_license_by_order(session, order_id)
        return {"ok": True, "delivered": True, "already": True,
                "license_id": lic.id if lic else None}

    if order.status not in ("approved", "provisioning_pending"):
        raise LicenseError("order is not approved", code="not_approved")

    # Reuse a license already reserved/sold for this order; else reserve one
    # (inline, without committing — see the docstring).
    lic = await get_license_by_order(session, order_id)
    reserved_now = False
    if lic is None:
        lic = await session.scalar(
            select(LicenseItem)
            .where(LicenseItem.product_id == product.id,
                   LicenseItem.status == "available")
            .order_by(LicenseItem.id)
            .limit(1)
            .with_for_update(skip_locked=True)
        )
        if lic is None:
            order.delivery_error = "no license available in stock"
            await audit_service.log(
                session, actor_type="admin", actor_id=actor_id,
                action="license_delivery_failed", target_type="order", target_id=order.id,
                meta=f"order={order.order_number} reason=no_stock",
            )
            return {"ok": False, "delivered": False, "reason": "no_license_available"}
        lic.status = "reserved"
        lic.order_id = order_id
        lic.sold_to_user_id = order.user_id
        lic.reserved_at = _now()
        await session.flush()
        reserved_now = True

    sent = await _deliver_to_user(bot, order, product, lic, order.user.language if order.user else "fa")
    if not sent:
        order.delivery_error = "telegram delivery failed"
        if reserved_now:
            await audit_service.log(
                session, actor_type="system", actor_id=None, action="license_reserved",
                target_type="license", target_id=lic.id,
                meta=f"order_id={order_id} user_id={order.user_id}",
            )
        await audit_service.log(
            session, actor_type="admin", actor_id=actor_id,
            action="license_delivery_failed", target_type="order", target_id=order.id,
            meta=f"order={order.order_number} reason=send_failed license_id={lic.id}",
        )
        return {"ok": False, "delivered": False, "reason": "delivery_failed",
                "license_id": lic.id}

    lic.status = "sold"
    lic.sold_at = _now()
    order.status = "delivered"
    order.delivered_at = _now()
    order.delivery_error = None
    order.delivered_payload = f"license #{lic.id} · {lic.email}"
    if reserved_now:
        await audit_service.log(
            session, actor_type="system", actor_id=None, action="license_reserved",
            target_type="license", target_id=lic.id,
            meta=f"order_id={order_id} user_id={order.user_id}",
        )
    await audit_service.log(
        session, actor_type="admin", actor_id=actor_id, action="license_sold",
        target_type="license", target_id=lic.id,
        meta=f"order={order.order_number} email={mask_email(lic.email)}",
    )
    await audit_service.log(
        session, actor_type="admin", actor_id=actor_id, action="license_delivered",
        target_type="order", target_id=order.id, meta=f"order={order.order_number}",
    )
    await _record_low_stock(session, product.id)
    return {"ok": True, "delivered": True, "license_id": lic.id}


async def redeliver_license(
    session: AsyncSession, order_id: int, *, bot=None, admin_id: int | None = None,
) -> dict:
    """Re-send the license for an order to its buyer.

    - ``sold``: just re-send the credentials (no new sale).
    - ``reserved``: a license whose first delivery failed and was left stranded —
      finish the interrupted delivery (promotes it to ``sold`` on a successful
      send). This is the admin's recovery path for a Telegram send failure.
    """
    lic = await get_license_by_order(session, order_id)
    if lic is None or lic.status not in ("sold", "reserved"):
        raise LicenseError("no deliverable license for this order", code="no_sold_license")
    if lic.status == "reserved":
        return await deliver_license_for_order(
            session, order_id, bot=bot, actor_id=admin_id
        )
    order = await order_service.get_order(session, order_id)
    sent = await _deliver_to_user(bot, order, order.product, lic,
                                  order.user.language if order.user else "fa")
    if not sent:
        return {"ok": False, "reason": "delivery_failed", "license_id": lic.id}
    await audit_service.log(
        session, actor_type="admin", actor_id=admin_id, action="license_redelivered",
        target_type="license", target_id=lic.id,
        meta=f"order_id={order_id} email={mask_email(lic.email)}",
    )
    return {"ok": True, "redelivered": True, "license_id": lic.id}


async def mark_license_broken(
    session: AsyncSession, license_id: int, admin_id: int | None = None, reason: str | None = None,
) -> LicenseItem | None:
    lic = await get_license(session, license_id)
    if lic is None:
        return None
    lic.status = "broken"
    if reason:
        lic.admin_note = reason
    await audit_service.log(
        session, actor_type="admin", actor_id=admin_id, action="license_marked_broken",
        target_type="license", target_id=lic.id, meta=f"reason={reason or ''}",
    )
    return lic


async def block_license(
    session: AsyncSession, license_id: int, admin_id: int | None = None,
) -> LicenseItem | None:
    lic = await get_license(session, license_id)
    if lic is None:
        return None
    if lic.status == "sold":
        raise LicenseError("a sold license cannot be blocked", code="already_sold")
    lic.status = "blocked"
    await audit_service.log(
        session, actor_type="admin", actor_id=admin_id, action="license_blocked",
        target_type="license", target_id=lic.id,
    )
    return lic


async def replace_license(
    session: AsyncSession, order_id: int, new_license_id: int | None = None,
    admin_id: int | None = None, reason: str | None = None, bot=None,
) -> dict:
    """Swap the order's sold license for a fresh one (same product), notify the user."""
    order = await order_service.get_order(session, order_id)
    if order is None:
        raise LicenseError("order not found", code="order_not_found")
    if order.status != "delivered":
        raise LicenseError("order is not delivered", code="not_delivered")
    old = await get_license_by_order(session, order_id)
    if old is None or old.status != "sold":
        raise LicenseError("no sold license for this order", code="no_sold_license")

    if new_license_id is not None:
        new = await get_license(session, new_license_id)
        if new is None or new.product_id != order.product_id or new.status != "available":
            raise LicenseError("chosen license is not available for this product",
                               code="new_not_available")
    else:
        new = await session.scalar(
            select(LicenseItem)
            .where(LicenseItem.product_id == order.product_id,
                   LicenseItem.status == "available")
            .order_by(LicenseItem.id).limit(1).with_for_update(skip_locked=True)
        )
        if new is None:
            raise NoLicenseAvailableError()

    now = _now()
    new.status = "sold"
    new.order_id = order_id
    new.sold_to_user_id = order.user_id
    new.reserved_at = now
    new.sold_at = now
    old.status = "replaced"
    old.replaced_by_license_id = new.id
    if reason:
        old.admin_note = reason
    order.delivered_payload = f"license #{new.id} · {new.email}"
    await session.flush()

    sent = await _deliver_to_user(bot, order, order.product, new,
                                  order.user.language if order.user else "fa")
    await audit_service.log(
        session, actor_type="admin", actor_id=admin_id, action="license_replaced",
        target_type="order", target_id=order.id,
        meta=f"order={order.order_number} old_id={old.id} new_id={new.id} reason={reason or ''}",
    )
    return {"ok": True, "old_id": old.id, "new_id": new.id, "sent": sent}


async def _record_low_stock(session: AsyncSession, product_id: int) -> None:
    """After a sale, audit a low-stock crossing (UI shows the live warning)."""
    from app.core.settings_service import SettingsService
    threshold = await SettingsService(session).get_int("license_low_stock_threshold", 5)
    avail = await count_available(session, product_id)
    if avail < threshold:
        await audit_service.log(
            session, actor_type="system", actor_id=None, action="low_stock_detected",
            target_type="product", target_id=product_id,
            meta=f"available={avail} threshold={threshold}",
        )

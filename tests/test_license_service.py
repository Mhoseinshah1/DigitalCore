"""Phase 5: license parser, stock service, delivery, dispatcher, security."""
from __future__ import annotations

import pytest
from sqlalchemy import select

from app.models import AuditLog, Order, Product, User
from app.services import (
    delivery_service,
    license_service,
    order_service,
    payment_service,
)
from app.services.license_service import LicenseError, NoLicenseAvailableError
from app.services.payment_service import ReceiptFile


@pytest.fixture(autouse=True)
def _stub_delivery(monkeypatch):
    async def _ok(bot, order, product, lic, lang="fa"):
        return True
    monkeypatch.setattr(license_service, "_deliver_to_user", _ok)


async def _lic_product(db_session, title="Netflix") -> Product:
    p = Product(type="license", title=title, price=50_000, is_active=True, is_hidden=False)
    db_session.add(p)
    await db_session.flush()
    return p


async def _submitted_license_order(db_session, tmp_path, tg=100):
    payment_service.RECEIPTS_ROOT = tmp_path
    u = User(telegram_id=tg, first_name="B", language="fa")
    db_session.add(u)
    p = await _lic_product(db_session)
    await db_session.flush()
    order = await order_service.create_order(db_session, u.id, p.id)
    await payment_service.create_payment_for_order(db_session, order)
    fi = ReceiptFile(content=b"\x89PNG\r\n\x1a\n" + b"x" * 20,
                     original_name="r.png", mime_type="image/png", file_id="f")
    await payment_service.submit_receipt(db_session, order.id, u.id, fi)
    return u, p, order


# --- parser -----------------------------------------------------------------
def test_parse_single() -> None:
    items, errors = license_service.parse_license_text(
        "EMAIL: a@x.com\nPASSWORD: pass1\nNOTE: note1")
    assert not errors
    assert items == [{"email": "a@x.com", "password": "pass1", "note": "note1", "block": 1}]


def test_parse_multiple_and_lowercase() -> None:
    items, errors = license_service.parse_license_text(
        "email: a@x.com\npassword: p1\n\nEMAIL: b@x.com\nPASSWORD: p2")
    assert not errors
    assert [i["email"] for i in items] == ["a@x.com", "b@x.com"]
    assert items[0]["note"] is None


def test_parse_missing_email_and_password() -> None:
    items, errors = license_service.parse_license_text("PASSWORD: only")
    assert not items and len(errors) == 1
    items, errors = license_service.parse_license_text("EMAIL: only@x.com")
    assert not items and len(errors) == 1


def test_parse_csv() -> None:
    items, errors = license_service.parse_license_text("a@x.com,p1,n1\nb@x.com,p2")
    assert not errors
    assert items[0] == {"email": "a@x.com", "password": "p1", "note": "n1", "block": 1}
    assert items[1]["note"] is None


async def test_bulk_import_dup_in_file_and_db(db_session) -> None:
    p = await _lic_product(db_session)
    r = await license_service.bulk_import_licenses(db_session, p.id, """
EMAIL: a@x.com
PASSWORD: p1

EMAIL: a@x.com
PASSWORD: dup

EMAIL: b@x.com
PASSWORD: p2
""", admin_id=9)
    assert r["imported"] == 2 and r["duplicates_in_file"] == 1
    # Re-importing a@x.com is a DB duplicate.
    r2 = await license_service.bulk_import_licenses(
        db_session, p.id, "EMAIL: a@x.com\nPASSWORD: again", admin_id=9)
    assert r2["imported"] == 0 and r2["duplicates_in_db"] == 1


def test_parse_kv_block_with_stray_line_is_error() -> None:
    # A KV block with a valid email+password BUT an extra unrecognized line must
    # be reported, never silently dropped (which would import bad/partial data).
    items, errors = license_service.parse_license_text(
        "EMAIL: a@x.com\nPASSWORD: p1\ngarbage line here")
    assert not items
    assert len(errors) == 1 and "unrecognized" in errors[0]["error"]


def test_parse_kv_typo_key_is_error() -> None:
    # A misspelled key (`EMIAL:`) makes the whole block an error rather than a
    # license silently created with a missing email.
    items, errors = license_service.parse_license_text("EMIAL: a@x.com\nPASSWORD: p1")
    assert not items and len(errors) == 1


async def test_import_rejects_non_license_product(db_session) -> None:
    p = Product(type="v2ray", title="VPN", price=1000, duration_days=30, traffic_gb=10,
                is_active=True, is_hidden=False, xui_server_id=1, xui_inbound_id=1)
    db_session.add(p)
    await db_session.flush()
    with pytest.raises(LicenseError) as ei:
        await license_service.bulk_import_licenses(db_session, p.id, "EMAIL: a@x.com\nPASSWORD: p")
    assert ei.value.code == "not_license_product"


# --- reservation + delivery -------------------------------------------------
async def test_add_and_count_available(db_session) -> None:
    p = await _lic_product(db_session)
    await license_service.add_license(db_session, p.id, "a@x.com", "p1", admin_id=9)
    await license_service.add_license(db_session, p.id, "b@x.com", "p2", admin_id=9)
    assert await license_service.count_available(db_session, p.id) == 2


async def test_reserve_and_no_stock_raises(db_session) -> None:
    p = await _lic_product(db_session)
    await license_service.add_license(db_session, p.id, "a@x.com", "p1")
    lic = await license_service.reserve_available_license(db_session, p.id, order_id=1, user_id=2)
    assert lic.status == "reserved" and lic.order_id == 1 and lic.sold_to_user_id == 2
    with pytest.raises(NoLicenseAvailableError):
        await license_service.reserve_available_license(db_session, p.id, order_id=3, user_id=4)


async def test_deliver_license_for_approved_order(db_session, tmp_path) -> None:
    u, p, order = await _submitted_license_order(db_session, tmp_path)
    await license_service.add_license(db_session, p.id, "sold@x.com", "pw", admin_id=9)
    await db_session.commit()
    await payment_service.approve_payment(db_session, order.id, admin_id=None)
    await db_session.commit()
    o = await order_service.get_order(db_session, order.id)
    lic = await license_service.get_license_by_order(db_session, order.id)
    assert o.status == "delivered"
    assert lic.status == "sold" and lic.sold_to_user_id == u.id and lic.order_id == order.id


async def test_delivered_order_does_not_sell_second(db_session, tmp_path) -> None:
    u, p, order = await _submitted_license_order(db_session, tmp_path)
    await license_service.add_license(db_session, p.id, "one@x.com", "pw", admin_id=9)
    await license_service.add_license(db_session, p.id, "two@x.com", "pw", admin_id=9)
    await db_session.commit()
    await payment_service.approve_payment(db_session, order.id, admin_id=None)
    await db_session.commit()
    # Calling delivery again is idempotent — still 1 available (only one sold).
    result = await license_service.deliver_license_for_order(db_session, order.id)
    assert result.get("already") is True
    assert await license_service.count_available(db_session, p.id) == 1


async def test_redeliver_same_license(db_session, tmp_path, monkeypatch) -> None:
    sent = []
    async def rec(bot, order, product, lic, lang="fa"):
        sent.append(lic.id); return True
    monkeypatch.setattr(license_service, "_deliver_to_user", rec)
    u, p, order = await _submitted_license_order(db_session, tmp_path)
    await license_service.add_license(db_session, p.id, "x@x.com", "pw", admin_id=9)
    await db_session.commit()
    await payment_service.approve_payment(db_session, order.id, admin_id=None)
    await db_session.commit()
    first = list(sent)
    r = await license_service.redeliver_license(db_session, order.id, admin_id=9)
    assert r["ok"] and sent[-1] == first[-1]  # same license id re-sent


async def test_stranded_reserved_license_recovers_on_retry(db_session, tmp_path, monkeypatch) -> None:
    """A Telegram send failure leaves the license reserved; the admin retry
    (redeliver) finishes the interrupted delivery, reusing the SAME reservation."""
    calls = {"n": 0}
    async def flaky(bot, order, product, lic, lang="fa"):
        calls["n"] += 1
        return calls["n"] > 1  # fail the first send, succeed on the retry
    monkeypatch.setattr(license_service, "_deliver_to_user", flaky)

    u, p, order = await _submitted_license_order(db_session, tmp_path)
    await license_service.add_license(db_session, p.id, "s@x.com", "pw", admin_id=9)
    await license_service.add_license(db_session, p.id, "spare@x.com", "pw", admin_id=9)
    await db_session.commit()

    # Approve → delivery fails at the send; the license is left reserved, the
    # order flagged, and only ONE license consumed (the spare is untouched).
    await payment_service.approve_payment(db_session, order.id, admin_id=None)
    await db_session.commit()
    stranded = await license_service.get_license_by_order(db_session, order.id)
    o = await order_service.get_order(db_session, order.id)
    assert stranded.status == "reserved"
    assert o.status != "delivered" and o.delivery_error
    assert await license_service.count_available(db_session, p.id) == 1

    # Admin retry finishes delivery, reusing the same reserved license.
    r = await license_service.redeliver_license(db_session, order.id, admin_id=9)
    await db_session.commit()
    assert r["ok"] and r.get("delivered") is True and r["license_id"] == stranded.id
    done = await license_service.get_license(db_session, stranded.id)
    o2 = await order_service.get_order(db_session, order.id)
    assert done.status == "sold" and o2.status == "delivered" and not o2.delivery_error
    # Still exactly one license consumed — no second reservation on retry.
    assert await license_service.count_available(db_session, p.id) == 1


async def test_delivery_reuses_reserved_license_never_double_reserves(db_session, tmp_path, monkeypatch) -> None:
    """A second delivery for the SAME order reuses the already-reserved license
    instead of reserving a second one (the invariant the order-row lock protects)."""
    async def fail(bot, order, product, lic, lang="fa"):
        return False
    monkeypatch.setattr(license_service, "_deliver_to_user", fail)
    u, p, order = await _submitted_license_order(db_session, tmp_path)
    await license_service.add_license(db_session, p.id, "a@x.com", "pw", admin_id=9)
    await license_service.add_license(db_session, p.id, "b@x.com", "pw", admin_id=9)
    order.status = "approved"
    await db_session.flush()

    r1 = await license_service.deliver_license_for_order(db_session, order.id)
    r2 = await license_service.deliver_license_for_order(db_session, order.id)
    assert r1["license_id"] == r2["license_id"]  # same reservation reused
    assert await license_service.count_available(db_session, p.id) == 1  # only one taken


async def test_replacement_sells_new_marks_old(db_session, tmp_path) -> None:
    u, p, order = await _submitted_license_order(db_session, tmp_path)
    await license_service.add_license(db_session, p.id, "old@x.com", "pw", admin_id=9)
    await license_service.add_license(db_session, p.id, "new@x.com", "pw", admin_id=9)
    await db_session.commit()
    await payment_service.approve_payment(db_session, order.id, admin_id=None)
    await db_session.commit()
    old = await license_service.get_license_by_order(db_session, order.id)
    r = await license_service.replace_license(db_session, order.id, admin_id=9, reason="bad")
    await db_session.commit()
    new = await license_service.get_license_by_order(db_session, order.id)
    old_after = await license_service.get_license(db_session, old.id)
    assert r["ok"] and new.id != old.id and new.email == "new@x.com"
    assert old_after.status == "replaced" and old_after.replaced_by_license_id == new.id


async def test_concurrent_delivery_does_not_double_sell(db_session) -> None:
    """With a single license, a second reservation must fail (never double-sell)."""
    p = await _lic_product(db_session)
    await license_service.add_license(db_session, p.id, "only@x.com", "pw")
    await license_service.reserve_available_license(db_session, p.id, order_id=1, user_id=1)
    with pytest.raises(NoLicenseAvailableError):
        await license_service.reserve_available_license(db_session, p.id, order_id=2, user_id=2)


# --- dispatcher -------------------------------------------------------------
async def test_dispatcher_license_delivers(db_session, tmp_path) -> None:
    u, p, order = await _submitted_license_order(db_session, tmp_path)
    await license_service.add_license(db_session, p.id, "d@x.com", "pw", admin_id=9)
    order.status = "approved"
    await db_session.flush()
    result = await delivery_service.deliver_order(db_session, order)
    assert result["delivered"] is True


async def test_dispatcher_v2ray_placeholder(db_session) -> None:
    p = Product(type="v2ray", title="VPN", price=1000, duration_days=30, traffic_gb=10,
                is_active=True, is_hidden=False, xui_server_id=1, xui_inbound_id=1)
    u = User(telegram_id=9, first_name="B")
    db_session.add_all([p, u])
    await db_session.flush()
    order = Order(order_number="DC-X-1", user_id=u.id, product_id=p.id,
                  amount=1000, final_amount=1000, status="approved",
                  payment_method="card_to_card")
    db_session.add(order)
    await db_session.flush()
    await db_session.refresh(order)
    result = await delivery_service.deliver_order(db_session, order)
    assert result["reason"] == "provisioning_pending" and order.status == "provisioning_pending"


# --- security ---------------------------------------------------------------
async def test_audit_never_contains_password(db_session, tmp_path) -> None:
    u, p, order = await _submitted_license_order(db_session, tmp_path)
    await license_service.add_license(db_session, p.id, "s@x.com", "TOPSECRETpw", admin_id=9)
    await db_session.commit()
    await payment_service.approve_payment(db_session, order.id, admin_id=None)
    await db_session.commit()
    rows = (await db_session.execute(select(AuditLog))).scalars().all()
    blob = " ".join(f"{r.old_value} {r.new_value} {r.meta}" for r in rows)
    assert "TOPSECRETpw" not in blob

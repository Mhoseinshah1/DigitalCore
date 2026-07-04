"""product_service: CRUD, per-type validation, visibility, ordering, audit."""
from __future__ import annotations

import pytest
from sqlalchemy import select

from app.models import AuditLog
from app.schemas.product import ProductCreate, ProductUpdate
from app.services import product_service


def _license(**overrides) -> ProductCreate:
    payload = {"type": "license", "title": "Win11 Pro Key", "price": 150_000}
    payload.update(overrides)
    return ProductCreate(**payload)


def _v2ray(**overrides) -> ProductCreate:
    payload = {
        "type": "v2ray",
        "title": "30d / 50GB",
        "price": 90_000,
        "duration_days": 30,
        "traffic_gb": 50,
    }
    payload.update(overrides)
    return ProductCreate(**payload)


async def test_create_license_without_v2ray_fields(db_session) -> None:
    product = await product_service.create(db_session, _license())
    await db_session.commit()
    assert product.id is not None
    assert product.type == "license"
    assert product.duration_days is None and product.traffic_gb is None


async def test_create_v2ray_ok(db_session) -> None:
    product = await product_service.create(db_session, _v2ray())
    await db_session.commit()
    assert product.duration_days == 30 and product.traffic_gb == 50


@pytest.mark.parametrize(
    "overrides",
    [
        {"duration_days": None},
        {"duration_days": 0},
        {"traffic_gb": None},
        {"traffic_gb": 0},
    ],
)
async def test_v2ray_requires_duration_and_traffic(db_session, overrides) -> None:
    with pytest.raises(ValueError):
        await product_service.create(db_session, _v2ray(**overrides))


async def test_invalid_type_and_empty_title_rejected(db_session) -> None:
    with pytest.raises(ValueError):
        await product_service.create(db_session, _license(type="ebook"))
    with pytest.raises(ValueError):
        await product_service.create(db_session, _license(title="   "))


async def test_update_changes_fields_and_validates_merged_state(db_session) -> None:
    product = await product_service.create(db_session, _v2ray())
    await db_session.commit()

    updated = await product_service.update(
        db_session, product.id, ProductUpdate(price=120_000, title="30d / 50GB v2")
    )
    await db_session.commit()
    assert updated is not None
    assert updated.price == 120_000 and updated.title == "30d / 50GB v2"

    # Zeroing a required v2ray field on update must be rejected (merged validation).
    with pytest.raises(ValueError):
        await product_service.update(db_session, product.id, ProductUpdate(duration_days=0))

    assert await product_service.update(db_session, 424242, ProductUpdate(price=1)) is None


async def test_deactivate_and_hide_and_user_list_filtering(db_session) -> None:
    visible = await product_service.create(db_session, _license(title="Visible", sort_order=2))
    first = await product_service.create(db_session, _license(title="First", sort_order=1))
    inactive = await product_service.create(db_session, _license(title="Inactive"))
    hidden = await product_service.create(db_session, _license(title="Hidden"))
    await db_session.commit()

    await product_service.deactivate(db_session, inactive.id)
    await product_service.set_hidden(db_session, hidden.id, True)
    await db_session.commit()

    user_list = await product_service.list_for_user(db_session)
    titles = [p.title for p in user_list]
    assert titles == ["First", "Visible"]  # sort_order then id; inactive/hidden gone

    admin_list = await product_service.list_for_admin(db_session)
    assert len(admin_list) == 4  # admins see everything

    # Idempotent toggles.
    again = await product_service.deactivate(db_session, inactive.id)
    assert again is not None and again.is_active is False


async def test_audit_rows_written(db_session) -> None:
    product = await product_service.create(
        db_session, _license(title="Audited"), actor_type="admin", actor_id=7
    )
    await product_service.update(
        db_session, product.id, ProductUpdate(price=1), actor_type="admin", actor_id=7
    )
    await product_service.deactivate(
        db_session, product.id, actor_type="admin", actor_id=7
    )
    await db_session.commit()

    actions = [
        row.action
        for row in (
            (await db_session.execute(select(AuditLog).order_by(AuditLog.id))).scalars().all()
        )
        if row.target_type == "product"
    ]
    assert actions == ["product.created", "product.updated", "product.deactivated"]

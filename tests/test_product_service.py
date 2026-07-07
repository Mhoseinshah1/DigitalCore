"""product_service: CRUD, per-type validation, visibility, ordering, audit."""
from __future__ import annotations

import pytest
from sqlalchemy import select

from app.models import AuditLog, XuiInbound, XuiServer
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


async def _seed_binding(db_session, *, server_active=True, inbound_active=True):
    """Create an active XUI server + inbound; return (server_id, inbound_id)."""
    server = XuiServer(
        name="S1", base_url="http://p:2053", status="active", is_active=server_active
    )
    db_session.add(server)
    await db_session.flush()
    inbound = XuiInbound(
        server_id=server.id, inbound_id=1, remark="in", is_active=inbound_active
    )
    db_session.add(inbound)
    await db_session.flush()
    return server.id, inbound.id


async def test_create_license_without_v2ray_fields(db_session) -> None:
    product = await product_service.create(db_session, _license())
    await db_session.commit()
    assert product.id is not None
    assert product.type == "license"
    assert product.duration_days is None and product.traffic_gb is None


async def test_create_v2ray_ok(db_session) -> None:
    sid, iid = await _seed_binding(db_session)
    product = await product_service.create(
        db_session, _v2ray(xui_server_id=sid, xui_inbound_id=iid)
    )
    await db_session.commit()
    assert product.duration_days == 30 and product.traffic_gb == 50
    assert product.xui_server_id == sid and product.xui_inbound_id == iid


async def test_v2ray_requires_xui_binding(db_session) -> None:
    # No server/inbound at all → rejected at field level.
    with pytest.raises(ValueError):
        await product_service.create(db_session, _v2ray())
    # Server but no inbound → rejected.
    sid, _iid = await _seed_binding(db_session)
    with pytest.raises(ValueError):
        await product_service.create(db_session, _v2ray(xui_server_id=sid))


async def test_license_must_not_set_xui_binding(db_session) -> None:
    sid, iid = await _seed_binding(db_session)
    with pytest.raises(ValueError):
        await product_service.create(
            db_session, _license(xui_server_id=sid, xui_inbound_id=iid)
        )


async def test_v2ray_inbound_must_belong_to_server(db_session) -> None:
    sid_a, _iid_a = await _seed_binding(db_session)
    sid_b, iid_b = await _seed_binding(db_session)  # inbound of a different server
    with pytest.raises(ValueError):
        await product_service.create(
            db_session, _v2ray(xui_server_id=sid_a, xui_inbound_id=iid_b)
        )


async def test_active_v2ray_needs_active_server_and_inbound(db_session) -> None:
    sid, iid = await _seed_binding(db_session, inbound_active=False)
    with pytest.raises(ValueError):
        await product_service.create(
            db_session,
            _v2ray(xui_server_id=sid, xui_inbound_id=iid, is_active=True),
        )
    # The same binding is fine for an INACTIVE product.
    product = await product_service.create(
        db_session,
        _v2ray(xui_server_id=sid, xui_inbound_id=iid, is_active=False),
    )
    await db_session.commit()
    assert product.is_active is False


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


# --- Phase 8: service-action products (renew / add-traffic) -----------------
def _renew(**overrides) -> ProductCreate:
    payload = {"type": "v2ray", "title": "Renew 30d", "price": 40_000,
               "duration_days": 30, "applies_to_service": True,
               "action_type": "renew_service"}
    payload.update(overrides)
    return ProductCreate(**payload)


def _add_traffic(**overrides) -> ProductCreate:
    payload = {"type": "v2ray", "title": "+20GB", "price": 20_000,
               "traffic_gb": 20, "applies_to_service": True,
               "action_type": "add_traffic"}
    payload.update(overrides)
    return ProductCreate(**payload)


async def test_create_renew_and_add_traffic_products(db_session) -> None:
    # Renew: needs a duration, no binding.
    renew = await product_service.create(db_session, _renew())
    add = await product_service.create(db_session, _add_traffic())
    await db_session.commit()
    assert renew.applies_to_service and renew.action_type == "renew_service"
    assert renew.xui_server_id is None and renew.xui_inbound_id is None
    assert add.applies_to_service and add.action_type == "add_traffic"
    # Excluded from the buy catalog, listed by the action query.
    assert renew.id not in {p.id for p in await product_service.list_for_user(db_session)}
    plans = await product_service.list_service_action_products(db_session, "renew_service")
    assert [p.id for p in plans] == [renew.id]


async def test_renew_requires_duration_add_traffic_requires_traffic(db_session) -> None:
    with pytest.raises(ValueError):
        await product_service.create(db_session, _renew(duration_days=0))
    with pytest.raises(ValueError):
        await product_service.create(db_session, _add_traffic(traffic_gb=None))


async def test_action_product_rejects_license_and_binding(db_session) -> None:
    # Only v2ray products can be service actions.
    with pytest.raises(ValueError):
        await product_service.create(db_session, _renew(type="license"))
    # A service action must not carry an XUI binding.
    sid, iid = await _seed_binding(db_session)
    with pytest.raises(ValueError):
        await product_service.create(
            db_session, _renew(xui_server_id=sid, xui_inbound_id=iid))
    # applies_to_service without an action_type is incomplete.
    with pytest.raises(ValueError):
        await product_service.create(
            db_session, ProductCreate(type="v2ray", title="x", price=1,
                                      duration_days=30, applies_to_service=True))


async def test_update_can_flip_product_to_service_action(db_session) -> None:
    sid, iid = await _seed_binding(db_session)
    product = await product_service.create(
        db_session, _v2ray(xui_server_id=sid, xui_inbound_id=iid))
    await db_session.commit()
    # Converting to a renewal action must drop the binding in the same update.
    updated = await product_service.update(
        db_session, product.id,
        ProductUpdate(applies_to_service=True, action_type="renew_service",
                      xui_server_id=None, xui_inbound_id=None))
    await db_session.commit()
    assert updated.applies_to_service and updated.action_type == "renew_service"


async def test_invalid_type_and_empty_title_rejected(db_session) -> None:
    with pytest.raises(ValueError):
        await product_service.create(db_session, _license(type="ebook"))
    with pytest.raises(ValueError):
        await product_service.create(db_session, _license(title="   "))


async def test_update_changes_fields_and_validates_merged_state(db_session) -> None:
    sid, iid = await _seed_binding(db_session)
    product = await product_service.create(
        db_session, _v2ray(xui_server_id=sid, xui_inbound_id=iid)
    )
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

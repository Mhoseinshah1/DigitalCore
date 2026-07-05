"""Delivery after approval (Phase 4).

- license: pop a code from the product's key pool and record it on the order.
- v2ray: best-effort provision a client on the bound 3X-UI server/inbound.

Both set the order to `delivered` with `delivered_payload` on success. If nothing
can be delivered (empty license pool, or a panel that is unreachable), the order
stays `approved` and the reason is returned so the caller can warn the admin.
This never raises — delivery failures must not undo an approval.
"""
from __future__ import annotations

import logging
import uuid
from datetime import datetime, timezone

from sqlalchemy.ext.asyncio import AsyncSession

from app.models.order import Order
from app.services import audit_service, license_service

log = logging.getLogger("delivery")


def _now() -> datetime:
    return datetime.now(timezone.utc)


async def _deliver_license(session: AsyncSession, order: Order, *, actor_id) -> dict:
    key = await license_service.assign_next(session, order.product_id, order.id)
    if key is None:
        return {"ok": False, "delivered": False, "reason": "no_license_keys"}
    order.delivered_payload = key.code
    return {"ok": True, "delivered": True, "payload": key.code}


async def _deliver_v2ray(session: AsyncSession, order: Order, *, actor_id) -> dict:
    """Best-effort: create a client on the panel. Never raises."""
    from app.services import xui_server_service, xui_service
    from app.xui.exceptions import XuiError
    from app.xui.schemas import ClientAdd

    product = order.product
    server = await xui_server_service.get_server(session, product.xui_server_id) \
        if product.xui_server_id else None
    inbound = await xui_server_service.get_inbound(session, product.xui_inbound_id) \
        if product.xui_inbound_id else None
    if server is None or inbound is None:
        return {"ok": False, "delivered": False, "reason": "missing_binding"}

    email = f"{order.order_number}".lower()
    client = ClientAdd(
        email=email,
        uuid=str(uuid.uuid4()),
        total_gb=int(product.traffic_gb or 0),
        limit_ip=int(product.ip_limit or 0),
        tg_id=str(order.user.telegram_id) if order.user and order.user.telegram_id else None,
    )
    try:
        await xui_service.add_client(server, inbound.inbound_id, client)
    except (XuiError, Exception) as exc:  # noqa: BLE001 - best-effort, never raise
        log.warning("V2Ray provisioning failed for order %s: %s", order.order_number, exc)
        return {"ok": False, "delivered": False, "reason": "provision_failed"}
    order.delivered_payload = f"email={email}"
    return {"ok": True, "delivered": True, "payload": email}


async def deliver_order(session: AsyncSession, order: Order, *, actor_id=None) -> dict:
    """Try to deliver an approved order. On success marks it delivered."""
    product = order.product
    if product is None:
        return {"ok": False, "delivered": False, "reason": "no_product"}

    if product.type == "license":
        result = await _deliver_license(session, order, actor_id=actor_id)
    elif product.type == "v2ray":
        result = await _deliver_v2ray(session, order, actor_id=actor_id)
    else:
        result = {"ok": False, "delivered": False, "reason": "unknown_type"}

    if result.get("delivered"):
        order.status = "delivered"
        order.delivered_at = _now()
        await audit_service.log(
            session, actor_type="admin", actor_id=actor_id, action="order_delivered",
            target_type="order", target_id=order.id,
            new=f"number={order.order_number} type={product.type}",
        )
    return result

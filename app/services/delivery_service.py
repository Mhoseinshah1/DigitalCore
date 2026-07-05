"""Delivery dispatcher: routes an approved order to its delivery path.

- license → the real license delivery (reserve + sell + send credentials); the
  order becomes `delivered` on success (see license_service).
- v2ray → placeholder only: the order is parked at `provisioning_pending`; no
  3X-UI client is created yet (Phase 6).

Never raises — a delivery failure must not undo an approval.
"""
from __future__ import annotations

import logging
from datetime import datetime, timezone

from sqlalchemy.ext.asyncio import AsyncSession

from app.models.order import Order
from app.services import audit_service, license_service

log = logging.getLogger("delivery")


def _now() -> datetime:
    return datetime.now(timezone.utc)


async def deliver_order(
    session: AsyncSession, order: Order, *, actor_id=None, bot=None
) -> dict:
    """Dispatch an approved order to its delivery handler."""
    product = order.product
    if product is None:
        return {"ok": False, "delivered": False, "reason": "no_product"}

    if product.type == "license":
        try:
            return await license_service.deliver_license_for_order(
                session, order.id, bot=bot, actor_id=actor_id
            )
        except license_service.LicenseError as exc:
            log.warning("License delivery error for order %s: %s", order.order_number, exc)
            return {"ok": False, "delivered": False, "reason": exc.code}

    if product.type == "v2ray":
        # Placeholder: no client creation yet — park at provisioning_pending.
        order.status = "provisioning_pending"
        await audit_service.log(
            session, actor_type="admin", actor_id=actor_id,
            action="order_provisioning_pending", target_type="order", target_id=order.id,
            new=f"number={order.order_number}",
        )
        return {"ok": True, "delivered": False, "reason": "provisioning_pending"}

    return {"ok": False, "delivered": False, "reason": "unknown_type"}

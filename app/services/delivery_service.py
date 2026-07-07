"""Delivery dispatcher: routes an approved order to its delivery path.

- license → the real license delivery (reserve + sell + send credentials); the
  order becomes `delivered` on success (see license_service).
- v2ray → real 3X-UI provisioning (Phase 6): create + verify a panel client,
  store a V2RayService, deliver the subscription link + QR; the order becomes
  `delivered` on success. On failure it stays approved/provisioning_pending with
  a safe delivery_error so an admin can retry (see v2ray_service).

Never raises — a delivery failure must not undo an approval.
"""
from __future__ import annotations

import logging
from datetime import datetime, timezone

from sqlalchemy.ext.asyncio import AsyncSession

from app.models.order import Order
from app.services import license_service, v2ray_lifecycle_service, v2ray_service

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
        # A renew/add-traffic order modifies an existing service (Phase 8); a
        # plain v2ray order provisions a new client (Phase 6).
        is_action = order.action_type in ("renew_service", "add_traffic")
        try:
            if is_action:
                result = await v2ray_lifecycle_service.apply_service_action_for_order(
                    session, order.id, actor_id=actor_id, bot=bot
                )
            else:
                result = await v2ray_service.provision_service_for_order(
                    session, order.id, actor_id=actor_id, bot=bot
                )
        except v2ray_service.V2RayError as exc:
            log.warning("V2Ray %s error for order %s: %s",
                        "action" if is_action else "provisioning", order.order_number, exc)
            return {"ok": False, "delivered": False, "reason": exc.code}
        # Normalize to the dispatcher's {ok, delivered, reason} shape.
        return {
            "ok": result.get("ok", False),
            "delivered": bool(result.get("provisioned")),
            "reason": result.get("reason",
                                 "provisioned" if result.get("provisioned") else "failed"),
            "service_id": result.get("service_id"),
            "already": result.get("already", False),
        }

    return {"ok": False, "delivered": False, "reason": "unknown_type"}

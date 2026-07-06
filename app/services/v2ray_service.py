"""Real 3X-UI V2Ray provisioning (Phase 6).

When an approved v2ray order is dispatched here we create ONE client on the
bound 3X-UI inbound, verify it exists (verify-after-write), store a local
`V2RayService`, and deliver the subscription link + QR to the buyer.

Idempotency / no-double-provision is guaranteed three ways:
  1. The order row is locked FOR UPDATE for the whole operation (no intermediate
     commit), so concurrent provisions for the same order serialize — the loser
     sees the active service and returns it.
  2. `order_id` is unique in `v2ray_services` (DB backstop).
  3. The client email is deterministic (`dc-u{user}-o{order}`), and we
     `find_client` on the panel before adding — so a retry after a partial run
     (panel write succeeded, DB write didn't) repairs the local row instead of
     creating a second panel client.

A provisioning failure never rolls back the payment approval: the order keeps
its approved/provisioning_pending status with a safe `delivery_error`, the
service row is marked `failed`, and an admin can retry. No XUI credential,
token, or cookie is ever stored or logged here.
"""
from __future__ import annotations

import logging
import re
import uuid
from datetime import datetime, timedelta, timezone

from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.models.audit_log import AuditLog
from app.models.order import Order
from app.models.v2ray_service import V2RayService
from app.models.xui_inbound import XuiInbound
from app.models.xui_server import XuiServer
from app.services import audit_service, order_service, subscription_service, xui_service
from app.xui.exceptions import XuiError, XuiVerificationError
from app.xui.schemas import ClientAdd, ClientUpdate

log = logging.getLogger("v2ray")

GB = 1024 ** 3


class V2RayError(ValueError):
    code = "v2ray_error"

    def __init__(self, message: str, code: str | None = None) -> None:
        super().__init__(message)
        if code is not None:
            self.code = code


def _now() -> datetime:
    return datetime.now(timezone.utc)


def _is_expired(expire_at: datetime | None) -> bool:
    """tz-safe expiry check (a naive value read back from SQLite is treated UTC)."""
    if expire_at is None:
        return False
    if expire_at.tzinfo is None:
        expire_at = expire_at.replace(tzinfo=timezone.utc)
    return expire_at < _now()


def _safe_error(message: str | None) -> str:
    """A short, credential-free error string for storage/audit."""
    return (str(message or "")[:200]).strip()


def _audit_nocommit(
    session: AsyncSession, action: str, *, actor_id: int | None = None,
    actor_type: str = "admin", target_type: str = "order",
    target_id: object = None, meta: str | None = None,
) -> None:
    """Add an audit row WITHOUT committing (keeps the order lock held).

    Provisioning writes its audit trail through this so the whole operation is a
    single transaction; `audit_service.log` (which commits) is only used by the
    post-commit, lock-free management/notification paths.
    """
    session.add(AuditLog(
        actor_type=actor_type, actor_id=actor_id, action=action,
        target_type=target_type,
        target_id=None if target_id is None else str(target_id),
        meta=None if meta is None else str(meta),
    ))


# --------------------------------------------------------------------------
# Deterministic identity + payload helpers
# --------------------------------------------------------------------------
def gb_to_bytes(gb: int | None) -> int:
    return int(gb or 0) * GB


def generate_client_uuid() -> str:
    return str(uuid.uuid4())


def generate_sub_id() -> str:
    return uuid.uuid4().hex[:16]


def _sanitize_email(raw: str) -> str:
    """Reduce to a 3X-UI-safe label (alnum, dash, dot, underscore)."""
    s = re.sub(r"[^A-Za-z0-9_.-]+", "-", (raw or "")).strip("-.").lower()
    return s or "client"


def generate_client_email(order, user, product) -> str:
    """Deterministic per order so a retry never mints a second panel client."""
    return _sanitize_email(f"dc-u{order.user_id}-o{order.order_number}")


def calculate_expire_at(product, start: datetime | None = None) -> datetime | None:
    days = int(product.duration_days or 0)
    if days <= 0:
        return None
    return (start or _now()) + timedelta(days=days)


def _expire_ms(expire_at: datetime | None) -> int:
    return int(expire_at.timestamp() * 1000) if expire_at else 0


def build_client_payload(order, product, user, client_email, client_uuid, sub_id) -> ClientAdd:
    return ClientAdd(
        email=client_email,
        uuid=client_uuid,
        enable=True,
        expiry_time=_expire_ms(calculate_expire_at(product)),
        total_gb=gb_to_bytes(product.traffic_gb),
        limit_ip=int(product.ip_limit or 1),
        sub_id=sub_id,
        tg_id=(str(user.telegram_id) if user and getattr(user, "telegram_id", None) else None),
    )


def _client_update(service: V2RayService, *, enable: bool = True) -> ClientUpdate:
    return ClientUpdate(
        email=service.client_email,
        uuid=service.client_uuid,
        enable=enable,
        expiry_time=_expire_ms(service.expire_at),
        total_gb=int(service.total_gb or 0),
        limit_ip=int(service.ip_limit or 0),
        sub_id=service.sub_id,
    )


# --------------------------------------------------------------------------
# Queries
# --------------------------------------------------------------------------
async def get_service(session: AsyncSession, service_id: int) -> V2RayService | None:
    return await session.get(V2RayService, service_id)


async def get_service_by_order(session: AsyncSession, order_id: int) -> V2RayService | None:
    return await session.scalar(
        select(V2RayService).where(V2RayService.order_id == order_id)
    )


async def list_user_services(
    session: AsyncSession, user_id: int, *, status: str | None = None, limit: int = 50
) -> list[V2RayService]:
    stmt = select(V2RayService).where(V2RayService.user_id == user_id)
    if status:
        stmt = stmt.where(V2RayService.status == status)
    stmt = stmt.order_by(V2RayService.id.desc()).limit(limit)
    return list((await session.execute(stmt)).scalars().all())


async def list_services(
    session: AsyncSession, *, status: str | None = None, server_id: int | None = None,
    product_id: int | None = None, limit: int = 50, offset: int = 0,
) -> list[V2RayService]:
    stmt = select(V2RayService)
    if status:
        stmt = stmt.where(V2RayService.status == status)
    if server_id:
        stmt = stmt.where(V2RayService.xui_server_id == server_id)
    if product_id:
        stmt = stmt.where(V2RayService.product_id == product_id)
    stmt = stmt.order_by(V2RayService.id.desc()).limit(limit).offset(offset)
    return list((await session.execute(stmt)).scalars().all())


async def count_by_status(session: AsyncSession) -> dict[str, int]:
    rows = await session.execute(
        select(V2RayService.status, func.count(V2RayService.id)).group_by(V2RayService.status)
    )
    return {status: count for status, count in rows.all()}


# --------------------------------------------------------------------------
# Provisioning
# --------------------------------------------------------------------------
async def _resolve_targets(session: AsyncSession, order) -> tuple[object, XuiServer, XuiInbound]:
    product = order.product
    if product is None or product.type != "v2ray":
        raise V2RayError("not a v2ray order", code="not_v2ray")
    if not product.xui_server_id or not product.xui_inbound_id:
        raise V2RayError("product is not bound to a server/inbound", code="product_unbound")
    if not product.duration_days or int(product.duration_days) <= 0:
        raise V2RayError("product duration is not set", code="no_duration")
    if product.traffic_gb is None or int(product.traffic_gb) < 0:
        raise V2RayError("product traffic is not set", code="no_traffic")

    server = await session.get(XuiServer, product.xui_server_id)
    if server is None:
        raise V2RayError("xui server not found", code="server_missing")
    if not server.is_active:
        raise V2RayError("xui server is inactive", code="server_inactive")

    inbound = await session.get(XuiInbound, product.xui_inbound_id)
    if inbound is None:
        raise V2RayError("xui inbound not found", code="inbound_missing")
    if inbound.server_id != server.id:
        raise V2RayError("inbound does not belong to the server", code="inbound_mismatch")
    if not inbound.is_active:
        raise V2RayError("xui inbound is inactive", code="inbound_inactive")
    return product, server, inbound


async def provision_service_for_order(
    session: AsyncSession, order_id: int, *, actor_id: int | None = None, bot=None,
    transport=None, sleep=None,
) -> dict:
    """Create + verify + deliver one 3X-UI client for an approved v2ray order.

    Idempotent per order. Holds the order row lock for the whole operation (no
    intermediate commit) so concurrent provisions serialize. Never raises on a
    provisioning failure — returns ``{"ok": False, ...}`` and leaves an admin-
    retryable failed service + order.delivery_error.
    """
    # Serialize concurrent same-order provisions on the order row.
    await session.execute(select(Order.id).where(Order.id == order_id).with_for_update())
    order = await order_service.get_order(session, order_id)
    if order is None:
        raise V2RayError("order not found", code="order_not_found")
    if order.product is None or order.product.type != "v2ray":
        raise V2RayError("not a v2ray order", code="not_v2ray")
    if order.status not in ("approved", "provisioning_pending", "delivered"):
        raise V2RayError("order is not approved", code="not_approved")

    existing = await get_service_by_order(session, order_id)
    if existing is not None and existing.status == "active":
        return {"ok": True, "provisioned": True, "already": True, "service_id": existing.id}

    # Validate the bound server/inbound. A validation failure is a safe,
    # retryable failure (no panel call attempted).
    try:
        product, server, inbound = await _resolve_targets(session, order)
    except V2RayError as exc:
        return await _mark_failed(session, order, existing, actor_id, exc.code, str(exc))

    retry = existing is not None
    if retry:
        service = existing
        client_email = service.client_email
        client_uuid = service.client_uuid
        sub_id = service.sub_id or generate_sub_id()
        service.sub_id = sub_id
        _audit_nocommit(session, "v2ray_provision_retry", actor_id=actor_id,
                        target_id=order.id,
                        meta=f"order={order.order_number} service_id={service.id}")
    else:
        client_email = generate_client_email(order, order.user, product)
        client_uuid = generate_client_uuid()
        sub_id = generate_sub_id()
        service = V2RayService(
            user_id=order.user_id, order_id=order.id, product_id=product.id,
            xui_server_id=server.id, xui_inbound_id=inbound.id,
            client_email=client_email, client_uuid=client_uuid, sub_id=sub_id,
            total_gb=gb_to_bytes(product.traffic_gb), used_gb=0,
            ip_limit=int(product.ip_limit or 1),
            expire_at=calculate_expire_at(product), status="provisioning",
        )
        session.add(service)
        await session.flush()

    service.status = "provisioning"
    service.last_error = None
    _audit_nocommit(session, "v2ray_provision_started", actor_id=actor_id, target_id=order.id,
                    meta=f"order={order.order_number} service_id={service.id} email={client_email}")

    payload = build_client_payload(order, product, order.user, client_email, client_uuid, sub_id)

    # Idempotent panel write: reuse a client left by a prior partial run.
    try:
        found = await xui_service.find_client(
            server, inbound.inbound_id, client_email, transport=transport, sleep=sleep
        )
        if found is None:
            await xui_service.add_client(
                server, inbound.inbound_id, payload, verify=True,
                transport=transport, sleep=sleep,
            )
            _audit_nocommit(session, "v2ray_client_created", actor_id=actor_id,
                            target_type="v2ray_service", target_id=service.id,
                            meta=f"order={order.order_number} email={client_email}")
        else:
            # A client left by a prior partial run (panel write was durable, the
            # local row was not). ADOPT its real identity so the subscription link
            # and every later management op key on the client that actually exists
            # on the panel — never the locally-regenerated uuid/sub_id — then
            # reconcile its config to the intended payload with verify-after-write
            # so the delivered service is guaranteed to match the panel.
            if found.uuid:
                client_uuid = found.uuid
            if found.sub_id:
                sub_id = found.sub_id
            service.client_uuid = client_uuid
            service.sub_id = sub_id
            reconcile = ClientUpdate(
                email=client_email, uuid=client_uuid, enable=True,
                expiry_time=payload.expiry_time, total_gb=payload.total_gb,
                limit_ip=payload.limit_ip, sub_id=sub_id,
            )
            await xui_service.update_client(
                server, inbound.inbound_id, reconcile, verify=True,
                transport=transport, sleep=sleep,
            )
        _audit_nocommit(session, "v2ray_client_verified", actor_id=actor_id,
                        target_type="v2ray_service", target_id=service.id,
                        meta=f"order={order.order_number} email={client_email}")
    except XuiVerificationError as exc:
        return await _mark_failed(session, order, service, actor_id, "verify_failed", str(exc),
                                  bot=bot)
    except XuiError as exc:
        return await _mark_failed(session, order, service, actor_id, "panel_error", str(exc),
                                  bot=bot)

    # Success — finalize the service + order in one atomic commit.
    now = _now()
    service.status = "active"
    service.provisioned_at = now
    service.subscription_url = subscription_service.build_subscription_url(server, sub_id)
    service.qr_code_path = (
        subscription_service.generate_qr_png(service.subscription_url, service.id)
        if service.subscription_url else None
    )
    order.status = "delivered"
    order.delivered_at = now
    order.delivery_error = None
    order.delivered_payload = f"V2Ray service #{service.id} · {client_email}"
    _audit_nocommit(session, "v2ray_service_delivered", actor_id=actor_id, target_id=order.id,
                    meta=f"order={order.order_number} service_id={service.id}")

    # Render the delivery message while the ORM objects are fresh.
    lang = order.user.language if order.user and order.user.language else "fa"
    message = build_service_message(order, product, service, lang)
    target = order.user.telegram_id if order.user else None
    qr_path = service.qr_code_path
    service_id = service.id
    await session.commit()  # releases the order lock; persists row+order+audit atomically

    # Best-effort delivery to the buyer (outside the lock). A send failure does
    # NOT undo provisioning — the service exists and is retrievable via /my_services.
    await _notify_user(session, service_id, order.id, target, message, qr_path, actor_id, bot)
    return {"ok": True, "provisioned": True, "service_id": service_id}


async def _mark_failed(
    session: AsyncSession, order, service: V2RayService | None, actor_id: int | None,
    code: str, message: str, *, bot=None,
) -> dict:
    """Record a provisioning failure (no approval rollback) and commit."""
    safe = _safe_error(message)
    if service is not None:
        service.status = "failed"
        service.last_error = safe
    # Keep the order approved/provisioning_pending so an admin can retry.
    if order.status == "approved":
        order.status = "provisioning_pending"
    order.delivery_error = f"v2ray_provision_failed:{code}"
    _audit_nocommit(session, "v2ray_provision_failed", actor_id=actor_id, target_id=order.id,
                    meta=f"order={order.order_number} reason={code}")
    lang = order.user.language if order.user and order.user.language else "fa"
    target = order.user.telegram_id if order.user else None
    fail_text = _failure_message(lang)
    order_id = order.id
    service_id = service.id if service is not None else None
    await session.commit()
    # Tell the buyer their payment is fine but the service build failed (best-effort).
    if target:
        await _send_text(bot, target, fail_text)
    return {"ok": False, "provisioned": False, "reason": code, "service_id": service_id}


async def retry_failed_provisioning(
    session: AsyncSession, order_id: int, *, actor_id: int | None = None, bot=None,
    transport=None, sleep=None,
) -> dict:
    """Admin-triggered retry — re-runs provisioning, reusing the deterministic
    identity so no duplicate panel client is created."""
    return await provision_service_for_order(
        session, order_id, actor_id=actor_id, bot=bot, transport=transport, sleep=sleep
    )


# --------------------------------------------------------------------------
# Management (post-provision, lock-free — each commits via audit_service.log)
# --------------------------------------------------------------------------
async def refresh_service_usage(
    session: AsyncSession, service_id: int, *, actor_id: int | None = None,
    transport=None, sleep=None,
) -> V2RayService | None:
    service = await get_service(session, service_id)
    if service is None:
        return None
    try:
        traffic = await xui_service.get_client_traffic(
            service.xui_server, service.client_email, transport=transport, sleep=sleep
        )
    except XuiError as exc:
        service.last_error = _safe_error(str(exc))
        await session.commit()
        return service
    service.used_gb = int((traffic.up or 0) + (traffic.down or 0))
    service.last_traffic_sync_at = _now()
    service.last_error = None
    if service.status == "active" and _is_expired(service.expire_at):
        service.status = "expired"
    await audit_service.log(
        session, actor_type="admin", actor_id=actor_id,
        action="v2ray_service_usage_refreshed", target_type="v2ray_service",
        target_id=service.id, meta=f"used_bytes={service.used_gb}",
    )
    return service


async def disable_service(
    session: AsyncSession, service_id: int, *, actor_id: int | None = None,
    transport=None, sleep=None,
) -> V2RayService:
    service = await _service_or_raise(session, service_id)
    try:
        await xui_service.set_client_enabled(
            service.xui_server, service.xui_inbound.inbound_id,
            _client_update(service, enable=False), False, transport=transport, sleep=sleep,
        )
    except XuiError as exc:
        raise V2RayError(_safe_error(str(exc)), code="panel_error") from exc
    service.status = "disabled"
    service.disabled_at = _now()
    await audit_service.log(
        session, actor_type="admin", actor_id=actor_id, action="v2ray_service_disabled",
        target_type="v2ray_service", target_id=service.id,
    )
    return service


async def enable_service(
    session: AsyncSession, service_id: int, *, actor_id: int | None = None,
    transport=None, sleep=None,
) -> V2RayService:
    service = await _service_or_raise(session, service_id)
    try:
        await xui_service.set_client_enabled(
            service.xui_server, service.xui_inbound.inbound_id,
            _client_update(service, enable=True), True, transport=transport, sleep=sleep,
        )
    except XuiError as exc:
        raise V2RayError(_safe_error(str(exc)), code="panel_error") from exc
    # Re-enabling an expired service leaves it expired; otherwise it is active.
    service.status = "expired" if _is_expired(service.expire_at) else "active"
    service.disabled_at = None
    await audit_service.log(
        session, actor_type="admin", actor_id=actor_id, action="v2ray_service_enabled",
        target_type="v2ray_service", target_id=service.id,
    )
    return service


async def delete_service(
    session: AsyncSession, service_id: int, *, actor_id: int | None = None,
    transport=None, sleep=None,
) -> V2RayService:
    service = await _service_or_raise(session, service_id)
    try:
        await xui_service.delete_client(
            service.xui_server, service.xui_inbound.inbound_id, service.client_uuid,
            transport=transport, sleep=sleep,
        )
    except XuiError as exc:
        raise V2RayError(_safe_error(str(exc)), code="panel_error") from exc
    service.status = "deleted"
    service.deleted_at = _now()
    await audit_service.log(
        session, actor_type="admin", actor_id=actor_id, action="v2ray_service_deleted",
        target_type="v2ray_service", target_id=service.id,
    )
    return service


async def reset_service_traffic(
    session: AsyncSession, service_id: int, *, actor_id: int | None = None,
    transport=None, sleep=None,
) -> V2RayService:
    service = await _service_or_raise(session, service_id)
    try:
        await xui_service.reset_client_traffic(
            service.xui_server, service.xui_inbound.inbound_id, service.client_email,
            transport=transport, sleep=sleep,
        )
    except XuiError as exc:
        raise V2RayError(_safe_error(str(exc)), code="panel_error") from exc
    service.used_gb = 0
    service.last_traffic_sync_at = _now()
    await audit_service.log(
        session, actor_type="admin", actor_id=actor_id, action="v2ray_traffic_reset",
        target_type="v2ray_service", target_id=service.id,
    )
    return service


async def _service_or_raise(session: AsyncSession, service_id: int) -> V2RayService:
    service = await get_service(session, service_id)
    if service is None:
        raise V2RayError("service not found", code="service_not_found")
    return service


# --------------------------------------------------------------------------
# Messaging
# --------------------------------------------------------------------------
def build_service_message(order, product, service: V2RayService, lang: str = "fa") -> str:
    from app.i18n import t
    expire = service.expire_at.strftime("%Y-%m-%d") if service.expire_at else "—"
    lines = [
        t("service.delivery.title", lang),
        "",
        t("service.delivery.order", lang, number=order.order_number),
        t("service.delivery.product", lang, title=product.title if product else "—"),
        t("service.delivery.duration", lang, days=product.duration_days or "—"),
        t("service.delivery.traffic", lang, gb=product.traffic_gb or "—"),
        t("service.delivery.ip_limit", lang, n=service.ip_limit or 1),
        t("service.delivery.expire", lang, date=expire),
    ]
    if service.subscription_url:
        lines += ["", t("service.delivery.sub_link", lang), f"<code>{service.subscription_url}</code>",
                  "", t("service.delivery.get_again", lang)]
    else:
        lines += ["", t("service.delivery.no_sub", lang)]
    return "\n".join(lines)


def _failure_message(lang: str = "fa") -> str:
    from app.i18n import t
    return t("service.delivery.failed", lang)


async def _send_text(bot, chat_id: int, text: str) -> bool:
    b, own = bot, None
    if b is None:
        from app.config import settings
        if not settings.TELEGRAM_BOT_TOKEN:
            return False
        from aiogram import Bot
        own = b = Bot(settings.TELEGRAM_BOT_TOKEN)
    try:
        await b.send_message(chat_id, text, parse_mode="HTML")
        return True
    except Exception as exc:  # noqa: BLE001 - a send failure is soft
        log.warning("V2Ray notification send failed: %s", exc)
        return False
    finally:
        if own is not None:
            try:
                await own.session.close()
            except Exception:  # noqa: BLE001
                pass


async def _notify_user(
    session: AsyncSession, service_id: int, order_id: int, target: int | None,
    message: str, qr_path: str | None, actor_id: int | None, bot,
) -> None:
    """Deliver the service message (+ QR photo) to the buyer; audit the outcome.
    Best-effort: a failure never undoes the (already committed) provisioning."""
    if not target:
        return
    b, own = bot, None
    if b is None:
        from app.config import settings
        if not settings.TELEGRAM_BOT_TOKEN:
            await _audit_notify(session, service_id, actor_id, ok=False, reason="no_bot")
            return
        from aiogram import Bot
        own = b = Bot(settings.TELEGRAM_BOT_TOKEN)
    ok = False
    try:
        await b.send_message(target, message, parse_mode="HTML")
        if qr_path:
            try:
                from aiogram.types import FSInputFile
                await b.send_photo(target, FSInputFile(qr_path))
            except Exception as exc:  # noqa: BLE001 - QR is a bonus, text already sent
                log.info("QR photo send skipped: %s", exc)
        ok = True
    except Exception as exc:  # noqa: BLE001
        log.warning("V2Ray delivery send failed for service %s: %s", service_id, exc)
    finally:
        if own is not None:
            try:
                await own.session.close()
            except Exception:  # noqa: BLE001
                pass
    await _audit_notify(session, service_id, actor_id, ok=ok, reason=None if ok else "send_failed")


async def _audit_notify(
    session: AsyncSession, service_id: int, actor_id: int | None, *, ok: bool, reason: str | None,
) -> None:
    await audit_service.log(
        session, actor_type="admin", actor_id=actor_id,
        action="v2ray_service_user_notified" if ok else "v2ray_service_notification_failed",
        target_type="v2ray_service", target_id=service_id,
        meta=None if ok else f"reason={reason}",
    )


# --------------------------------------------------------------------------
# Worker helpers (scheduler wiring is a documented TODO — see app/worker/main.py)
# --------------------------------------------------------------------------
async def mark_expired_services(session: AsyncSession) -> int:
    """Flip active services past their expiry to `expired`. Returns the count."""
    now = _now()
    rows = (await session.execute(
        select(V2RayService).where(
            V2RayService.status == "active",
            V2RayService.expire_at.is_not(None),
            V2RayService.expire_at < now,
        )
    )).scalars().all()
    for svc in rows:
        svc.status = "expired"
    if rows:
        await session.commit()
    return len(rows)

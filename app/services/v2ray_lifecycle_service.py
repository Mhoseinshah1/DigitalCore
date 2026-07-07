"""V2Ray service lifecycle (Phase 8): renewal, add-traffic, and the sweeps.

This layer sits on top of the Phase 6 provisioning service (`v2ray_service`) and
reuses its helpers (deterministic client identity, `_client_update`, the
`_audit_nocommit` single-transaction audit pattern, the safe notifier). It adds:

  * renewal — extend a service's expiry from ``max(now, current expiry)`` by the
    plan's duration and re-enable the panel client (verify-after-write);
  * add-traffic — grow a service's total quota by the plan's traffic and clear an
    over-quota state;
  * ``apply_service_action_for_order`` — the delivery entry point for a
    renew/add-traffic order, holding the order row lock for the whole operation
    (single terminal commit) so it is idempotent and never double-applies;
  * the worker sweeps — mark expired / over-quota, optionally auto-disable them on
    the panel, refresh usage from 3X-UI on an interval, and send one-shot expiry /
    traffic warnings.

Money is never touched here: a panel failure during a paid action leaves the
order retryable (``provisioning_pending`` + a safe ``delivery_error``) and never
rolls back the wallet charge / payment approval. No XUI credential is ever logged.
"""
from __future__ import annotations

import logging
from datetime import datetime, timedelta, timezone

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.settings_service import SettingsService
from app.models.order import Order
from app.models.v2ray_service import V2RayService
from app.services import audit_service, order_service, v2ray_service, xui_service
from app.services.v2ray_service import (
    V2RayError,
    _audit_nocommit,
    _is_expired,
    _now,
    _safe_error,
    _send_text,
    gb_to_bytes,
    get_service,
)
from app.xui.exceptions import XuiError, XuiVerificationError
from app.xui.schemas import ClientUpdate

log = logging.getLogger("v2ray.lifecycle")


# --------------------------------------------------------------------------
# Settings
# --------------------------------------------------------------------------
async def _usage_refresh_enabled(session: AsyncSession) -> bool:
    return await SettingsService(session).get_bool("v2ray_usage_refresh_enabled", True)


async def _usage_refresh_interval_minutes(session: AsyncSession) -> int:
    return await SettingsService(session).get_int("v2ray_usage_refresh_interval_minutes", 60)


async def _expiry_warning_days(session: AsyncSession) -> int:
    return await SettingsService(session).get_int("v2ray_expiry_warning_days", 3)


async def _traffic_warning_percent(session: AsyncSession) -> int:
    return await SettingsService(session).get_int("v2ray_traffic_warning_percent", 90)


async def _auto_disable_expired(session: AsyncSession) -> bool:
    return await SettingsService(session).get_bool("v2ray_auto_disable_expired", True)


async def _auto_disable_over_quota(session: AsyncSession) -> bool:
    return await SettingsService(session).get_bool("v2ray_auto_disable_over_quota", True)


# --------------------------------------------------------------------------
# Field math (pure)
# --------------------------------------------------------------------------
def _renewed_expiry(service: V2RayService, days: int) -> datetime:
    """New expiry = ``max(now, current expiry) + days`` (tz-safe)."""
    now = _now()
    current = service.expire_at
    if current is not None and current.tzinfo is None:
        current = current.replace(tzinfo=timezone.utc)
    base = max(now, current) if current is not None else now
    return base + timedelta(days=int(days))


def _added_total(service: V2RayService, gb: int) -> int:
    """New total quota in bytes. Unlimited (0) stays unlimited."""
    current = int(service.total_gb or 0)
    if current <= 0:
        return 0
    return current + gb_to_bytes(gb)


def remaining_bytes(service: V2RayService) -> int | None:
    """Bytes left before the quota (None when unlimited). Never negative."""
    total = int(service.total_gb or 0)
    if total <= 0:
        return None
    return max(0, total - int(service.used_gb or 0))


def remaining_days(service: V2RayService) -> int | None:
    """Days left before expiry, rounded up (None when no expiry). Never negative."""
    if service.expire_at is None:
        return None
    expire = service.expire_at
    if expire.tzinfo is None:
        expire = expire.replace(tzinfo=timezone.utc)
    delta = expire - _now()
    if delta.total_seconds() <= 0:
        return 0
    return delta.days + (1 if (delta.seconds or delta.microseconds) else 0)


def _is_over_quota(service: V2RayService) -> bool:
    total = int(service.total_gb or 0)
    return total > 0 and int(service.used_gb or 0) >= total


def _recompute_after_action(service: V2RayService) -> None:
    """Set status/stamps from the service's (already-updated) expiry+usage.

    Called after a successful renew/add-traffic panel write. Re-enables the local
    lifecycle state: a still-valid service is `active`; warning stamps reset so a
    fresh cycle can warn again."""
    now = _now()
    service.disabled_at = None
    if _is_expired(service.expire_at):
        service.status = "expired"
        service.expired_at = service.expired_at or now
    elif _is_over_quota(service):
        service.status = "over_quota"
        service.over_quota_at = service.over_quota_at or now
        service.last_expiry_warning_at = None  # expiry window is fresh again
    else:
        service.status = "active"
        service.expired_at = None
        service.over_quota_at = None
        service.last_expiry_warning_at = None
        service.last_traffic_warning_at = None


def _client_update_for(
    service: V2RayService, *, expire_at: datetime | None, total_bytes: int, enable: bool = True
) -> ClientUpdate:
    """Build a panel ClientUpdate from the service identity + target fields."""
    return ClientUpdate(
        email=service.client_email,
        uuid=service.client_uuid,
        enable=enable,
        expiry_time=v2ray_service._expire_ms(expire_at),
        total_gb=int(total_bytes or 0),
        limit_ip=int(service.ip_limit or 0),
        sub_id=service.sub_id,
    )


async def _apply_on_panel(
    service: V2RayService, *, action: str, duration_days: int, traffic_gb: int,
    transport=None, sleep=None,
) -> tuple[datetime | None, int]:
    """Push the renew/add-traffic change to the panel (verify-after-write).

    Computes the target expiry/total as locals and writes them WITHOUT mutating
    the ORM object, so a panel failure (which raises) leaves the local service
    untouched. Returns the new (expire_at, total_bytes) on success."""
    new_expire = service.expire_at
    new_total = int(service.total_gb or 0)
    if action == "renew_service":
        new_expire = _renewed_expiry(service, duration_days)
    elif action == "add_traffic":
        new_total = _added_total(service, traffic_gb)
    update = _client_update_for(service, expire_at=new_expire, total_bytes=new_total, enable=True)
    await xui_service.update_client(
        service.xui_server, service.xui_inbound.inbound_id, update,
        verify=True, transport=transport, sleep=sleep,
    )
    return new_expire, new_total


# --------------------------------------------------------------------------
# Usage refresh (delegates to the Phase 6 syncer, which now detects over-quota)
# --------------------------------------------------------------------------
async def refresh_usage(
    session: AsyncSession, service_id: int, *, actor_id: int | None = None,
    transport=None, sleep=None,
) -> V2RayService | None:
    """Sync used bytes from 3X-UI and reconcile expiry/over-quota. Commits."""
    return await v2ray_service.refresh_service_usage(
        session, service_id, actor_id=actor_id, transport=transport, sleep=sleep
    )


# --------------------------------------------------------------------------
# Admin-triggered renew / add-traffic (direct, no order — each commits)
# --------------------------------------------------------------------------
async def renew_service(
    session: AsyncSession, service_id: int, *, duration_days: int,
    actor_id: int | None = None, transport=None, sleep=None,
) -> V2RayService:
    """Extend a service's expiry by ``duration_days`` and re-enable it. Commits."""
    service = await v2ray_service._service_or_raise(session, service_id)
    if service.status == "deleted":
        raise V2RayError("service is deleted", code="service_deleted")
    if int(duration_days) <= 0:
        raise V2RayError("duration must be positive", code="bad_duration")
    try:
        new_expire, new_total = await _apply_on_panel(
            service, action="renew_service", duration_days=int(duration_days), traffic_gb=0,
            transport=transport, sleep=sleep,
        )
    except XuiError as exc:
        raise V2RayError(_safe_error(str(exc)), code="panel_error") from exc
    service.expire_at = new_expire
    _recompute_after_action(service)
    await audit_service.log(
        session, actor_type="admin", actor_id=actor_id, action="v2ray_service_renewed",
        target_type="v2ray_service", target_id=service.id,
        meta=f"days={int(duration_days)} status={service.status}",
    )
    return service


async def add_traffic(
    session: AsyncSession, service_id: int, *, traffic_gb: int,
    actor_id: int | None = None, transport=None, sleep=None,
) -> V2RayService:
    """Grow a service's total quota by ``traffic_gb`` and clear over-quota. Commits."""
    service = await v2ray_service._service_or_raise(session, service_id)
    if service.status == "deleted":
        raise V2RayError("service is deleted", code="service_deleted")
    if int(traffic_gb) <= 0:
        raise V2RayError("traffic must be positive", code="bad_traffic")
    try:
        new_expire, new_total = await _apply_on_panel(
            service, action="add_traffic", duration_days=0, traffic_gb=int(traffic_gb),
            transport=transport, sleep=sleep,
        )
    except XuiError as exc:
        raise V2RayError(_safe_error(str(exc)), code="panel_error") from exc
    service.total_gb = new_total
    _recompute_after_action(service)
    await audit_service.log(
        session, actor_type="admin", actor_id=actor_id, action="v2ray_traffic_added",
        target_type="v2ray_service", target_id=service.id,
        meta=f"gb={int(traffic_gb)} total_bytes={new_total} status={service.status}",
    )
    return service


# --------------------------------------------------------------------------
# Management delegates (kept namespaced for the web/bot lifecycle callers)
# --------------------------------------------------------------------------
async def disable_service(session: AsyncSession, service_id: int, **kw) -> V2RayService:
    return await v2ray_service.disable_service(session, service_id, **kw)


async def enable_service(session: AsyncSession, service_id: int, **kw) -> V2RayService:
    return await v2ray_service.enable_service(session, service_id, **kw)


async def reset_traffic(session: AsyncSession, service_id: int, **kw) -> V2RayService:
    return await v2ray_service.reset_service_traffic(session, service_id, **kw)


# --------------------------------------------------------------------------
# Order-driven action delivery (locked, idempotent, payment-safe)
# --------------------------------------------------------------------------
async def apply_service_action_for_order(
    session: AsyncSession, order_id: int, *, actor_id: int | None = None, bot=None,
    transport=None, sleep=None,
) -> dict:
    """Apply a paid renew/add-traffic order to its target service.

    Holds the order row lock for the whole operation (no intermediate commit) so
    concurrent deliveries serialize and a redelivery is a no-op. A panel failure
    never rolls back the payment — the order stays ``provisioning_pending`` with a
    safe ``delivery_error`` and is admin-retryable."""
    await session.execute(select(Order.id).where(Order.id == order_id).with_for_update())
    order = await order_service.get_order(session, order_id)
    if order is None:
        raise V2RayError("order not found", code="order_not_found")
    product = order.product
    if product is None or product.type != "v2ray":
        raise V2RayError("not a v2ray order", code="not_v2ray")
    action = order.action_type
    if action not in ("renew_service", "add_traffic"):
        raise V2RayError("not a service action", code="not_action")
    if order.status not in ("approved", "provisioning_pending", "delivered"):
        raise V2RayError("order is not approved", code="not_approved")

    # Idempotent: an already-delivered action order never re-applies.
    if order.status == "delivered":
        return {"ok": True, "provisioned": True, "already": True,
                "service_id": order.target_service_id}

    service = (await get_service(session, order.target_service_id)
               if order.target_service_id else None)
    if service is None:
        return await _fail_action(session, order, actor_id, "service_missing",
                                  "target service not found", bot=bot)
    if service.user_id != order.user_id:
        return await _fail_action(session, order, actor_id, "not_owner",
                                  "service owner mismatch", bot=bot)
    if service.status == "deleted":
        return await _fail_action(session, order, actor_id, "service_deleted",
                                  "service is deleted", bot=bot)
    if action == "renew_service" and int(product.duration_days or 0) <= 0:
        return await _fail_action(session, order, actor_id, "no_duration",
                                  "renewal product has no duration", bot=bot)
    if action == "add_traffic" and int(product.traffic_gb or 0) <= 0:
        return await _fail_action(session, order, actor_id, "no_traffic",
                                  "add-traffic product has no traffic", bot=bot)

    _audit_nocommit(session, "v2ray_action_started", actor_id=actor_id, target_id=order.id,
                    meta=f"order={order.order_number} action={action} service_id={service.id}")
    try:
        new_expire, new_total = await _apply_on_panel(
            service, action=action, duration_days=int(product.duration_days or 0),
            traffic_gb=int(product.traffic_gb or 0), transport=transport, sleep=sleep,
        )
    except XuiVerificationError as exc:
        return await _fail_action(session, order, actor_id, "verify_failed", str(exc), bot=bot)
    except XuiError as exc:
        return await _fail_action(session, order, actor_id, "panel_error", str(exc), bot=bot)

    # Success — mutate the service, mark the order delivered, one atomic commit.
    now = _now()
    if action == "renew_service":
        service.expire_at = new_expire
    else:
        service.total_gb = new_total
    _recompute_after_action(service)
    order.status = "delivered"
    order.delivered_at = now
    order.delivery_error = None
    order.delivered_payload = f"{action} · service #{service.id}"
    _audit_nocommit(session, "v2ray_action_delivered", actor_id=actor_id, target_id=order.id,
                    meta=f"order={order.order_number} action={action} service_id={service.id}")

    lang = order.user.language if order.user and order.user.language else "fa"
    message = build_action_message(order, product, service, action, lang)
    target = order.user.telegram_id if order.user else None
    service_id = service.id
    await session.commit()  # releases the lock; persists service+order+audit atomically

    if target:
        ok = await _send_text(bot, target, message)
        await audit_service.log(
            session, actor_type="user", actor_id=actor_id,
            action="v2ray_action_user_notified" if ok else "v2ray_action_notification_failed",
            target_type="v2ray_service", target_id=service_id,
        )
    return {"ok": True, "provisioned": True, "service_id": service_id, "action": action}


async def _fail_action(
    session: AsyncSession, order: Order, actor_id: int | None, code: str, message: str, *, bot=None,
) -> dict:
    """Record an action-delivery failure (no payment rollback) and commit.

    The target service is never mutated on failure, so it stays consistent with
    the panel. The order stays retryable."""
    if order.status == "approved":
        order.status = "provisioning_pending"
    order.delivery_error = f"v2ray_action_failed:{code}"
    _audit_nocommit(session, "v2ray_action_failed", actor_id=actor_id, target_id=order.id,
                    meta=f"order={order.order_number} reason={code} detail={_safe_error(message)}")
    lang = order.user.language if order.user and order.user.language else "fa"
    target = order.user.telegram_id if order.user else None
    await session.commit()
    if target:
        from app.i18n import t
        await _send_text(bot, target, t("service.action.failed", lang))
    return {"ok": False, "provisioned": False, "reason": code}


async def retry_action_for_order(
    session: AsyncSession, order_id: int, *, actor_id: int | None = None, bot=None,
    transport=None, sleep=None,
) -> dict:
    """Admin-triggered retry of a failed renew/add-traffic delivery."""
    return await apply_service_action_for_order(
        session, order_id, actor_id=actor_id, bot=bot, transport=transport, sleep=sleep
    )


# --------------------------------------------------------------------------
# Worker sweeps — DB-only marking
# --------------------------------------------------------------------------
async def mark_expired_services(session: AsyncSession) -> int:
    """Flip active/over-quota services past expiry to `expired`. DB-only."""
    return await v2ray_service.mark_expired_services(session)


async def mark_over_quota_services(session: AsyncSession) -> int:
    """Flip active services that reached their quota to `over_quota`. DB-only.

    Uses the already-synced ``used_gb`` (no panel call), so it is safe to run on
    every worker tick."""
    now = _now()
    rows = (await session.execute(
        select(V2RayService).where(
            V2RayService.status == "active",
            V2RayService.total_gb > 0,
            V2RayService.used_gb >= V2RayService.total_gb,
        )
    )).scalars().all()
    for svc in rows:
        svc.status = "over_quota"
        svc.over_quota_at = svc.over_quota_at or now
    if rows:
        await session.commit()
    return len(rows)


# --------------------------------------------------------------------------
# Worker sweeps — optional panel auto-disable (batched, idempotent, non-spammy)
# --------------------------------------------------------------------------
async def _disable_on_panel(
    session: AsyncSession, statuses: tuple[str, ...], *, reason: str, limit: int,
    transport=None, sleep=None,
) -> int:
    """Disable the panel client for services in `statuses` not yet disabled.

    Idempotent via the ``disabled_at IS NULL`` guard (so a service is disabled at
    most once), batched via ``limit``, and error-isolated per service so one bad
    panel never blocks the rest or crashes the worker."""
    rows = (await session.execute(
        select(V2RayService).where(
            V2RayService.status.in_(statuses),
            V2RayService.disabled_at.is_(None),
        ).order_by(V2RayService.id).limit(limit)
    )).scalars().all()
    done = 0
    for svc in rows:
        try:
            await xui_service.set_client_enabled(
                svc.xui_server, svc.xui_inbound.inbound_id,
                _client_update_for(svc, expire_at=svc.expire_at,
                                   total_bytes=int(svc.total_gb or 0), enable=False),
                False, transport=transport, sleep=sleep,
            )
        except XuiError as exc:
            svc.last_error = _safe_error(str(exc))
            log.warning("auto-disable (%s) failed for service %s: %s", reason, svc.id, exc)
            continue
        svc.disabled_at = _now()
        svc.last_error = None
        await audit_service.log(
            session, actor_type="system", actor_id=None,
            action="v2ray_service_auto_disabled", target_type="v2ray_service",
            target_id=svc.id, meta=f"reason={reason}",
        )
        done += 1
    return done


async def disable_expired_services(
    session: AsyncSession, *, limit: int = 50, transport=None, sleep=None,
) -> int:
    """Disable the panel client for `expired` services (if the setting is on)."""
    if not await _auto_disable_expired(session):
        return 0
    return await _disable_on_panel(session, ("expired",), reason="expired", limit=limit,
                                   transport=transport, sleep=sleep)


async def disable_over_quota_services(
    session: AsyncSession, *, limit: int = 50, transport=None, sleep=None,
) -> int:
    """Disable the panel client for `over_quota` services (if the setting is on)."""
    if not await _auto_disable_over_quota(session):
        return 0
    return await _disable_on_panel(session, ("over_quota",), reason="over_quota", limit=limit,
                                   transport=transport, sleep=sleep)


# --------------------------------------------------------------------------
# Worker sweeps — usage refresh on an interval (batched, panel calls)
# --------------------------------------------------------------------------
async def refresh_due_services(
    session: AsyncSession, *, limit: int = 25, transport=None, sleep=None,
) -> int:
    """Refresh usage for active/over-quota services whose last sync is stale.

    Interval-gated per service via ``last_traffic_sync_at`` (so it survives worker
    restarts without spamming the panel) and batched via ``limit``. Returns the
    number of services refreshed."""
    if not await _usage_refresh_enabled(session):
        return 0
    interval = max(1, await _usage_refresh_interval_minutes(session))
    cutoff = _now() - timedelta(minutes=interval)
    rows = (await session.execute(
        select(V2RayService.id).where(
            V2RayService.status.in_(("active", "over_quota")),
            (V2RayService.last_traffic_sync_at.is_(None))
            | (V2RayService.last_traffic_sync_at < cutoff),
        ).order_by(V2RayService.last_traffic_sync_at.asc().nulls_first()).limit(limit)
    )).scalars().all()
    done = 0
    for sid in rows:
        try:
            await v2ray_service.refresh_service_usage(
                session, sid, transport=transport, sleep=sleep)
            done += 1
        except Exception as exc:  # noqa: BLE001 - error isolation per service
            log.warning("usage refresh failed for service %s: %s", sid, exc)
    return done


# --------------------------------------------------------------------------
# Worker sweeps — one-shot warnings
# --------------------------------------------------------------------------
async def send_expiry_warnings(
    session: AsyncSession, *, limit: int = 100, bot=None,
) -> int:
    """Warn owners of active services expiring within the warning window, once.

    Guarded by ``last_expiry_warning_at IS NULL`` so a service is warned at most
    once as it approaches expiry; the stamp resets on renewal so a later cycle can
    warn again."""
    days = await _expiry_warning_days(session)
    if days <= 0:
        return 0
    now = _now()
    horizon = now + timedelta(days=days)
    rows = (await session.execute(
        select(V2RayService).where(
            V2RayService.status == "active",
            V2RayService.expire_at.is_not(None),
            V2RayService.expire_at > now,
            V2RayService.expire_at <= horizon,
            V2RayService.last_expiry_warning_at.is_(None),
        ).order_by(V2RayService.expire_at.asc()).limit(limit)
    )).scalars().all()
    sent = 0
    for svc in rows:
        left = remaining_days(svc)
        lang = svc.user.language if svc.user and svc.user.language else "fa"
        target = svc.user.telegram_id if svc.user else None
        svc.last_expiry_warning_at = now
        if target:
            from app.i18n import t
            await _send_text(bot, target, t("service.warn.expiry", lang,
                                            days=left if left is not None else 0,
                                            title=(svc.product.title if svc.product else "—")))
            sent += 1
    if rows:
        await session.commit()
    return sent


async def send_traffic_warnings(
    session: AsyncSession, *, limit: int = 100, bot=None,
) -> int:
    """Warn owners of active services past the traffic-warning percent, once.

    Guarded by ``last_traffic_warning_at IS NULL``; the stamp resets on
    add-traffic / traffic-reset so a later crossing can warn again."""
    percent = await _traffic_warning_percent(session)
    if percent <= 0 or percent >= 100:
        return 0
    now = _now()
    rows = (await session.execute(
        select(V2RayService).where(
            V2RayService.status == "active",
            V2RayService.total_gb > 0,
            V2RayService.used_gb < V2RayService.total_gb,
            V2RayService.used_gb * 100 >= V2RayService.total_gb * percent,
            V2RayService.last_traffic_warning_at.is_(None),
        ).order_by(V2RayService.id).limit(limit)
    )).scalars().all()
    sent = 0
    for svc in rows:
        lang = svc.user.language if svc.user and svc.user.language else "fa"
        target = svc.user.telegram_id if svc.user else None
        used_pct = int(int(svc.used_gb or 0) * 100 / int(svc.total_gb or 1))
        svc.last_traffic_warning_at = now
        if target:
            from app.i18n import t
            await _send_text(bot, target, t("service.warn.traffic", lang, percent=used_pct,
                                            title=(svc.product.title if svc.product else "—")))
            sent += 1
    if rows:
        await session.commit()
    return sent


# --------------------------------------------------------------------------
# Worker tick — one orchestrated pass (error-isolated at each step)
# --------------------------------------------------------------------------
async def lifecycle_tick(session: AsyncSession, *, bot=None, transport=None, sleep=None) -> dict:
    """Run one full lifecycle pass. Never raises — each step is isolated so a
    single failure (e.g. a panel timeout) never blocks the others."""
    out: dict[str, int] = {}

    async def _step(name: str, coro):
        try:
            out[name] = await coro
        except Exception as exc:  # noqa: BLE001 - isolation
            log.warning("lifecycle step %s failed: %s", name, exc)
            out[name] = 0

    await _step("expired", mark_expired_services(session))
    await _step("over_quota", mark_over_quota_services(session))
    await _step("refreshed", refresh_due_services(session, transport=transport, sleep=sleep))
    # Re-mark over-quota after a refresh so freshly-synced usage is acted on.
    await _step("over_quota_post", mark_over_quota_services(session))
    await _step("disabled_expired",
                disable_expired_services(session, transport=transport, sleep=sleep))
    await _step("disabled_over_quota",
                disable_over_quota_services(session, transport=transport, sleep=sleep))
    await _step("expiry_warnings", send_expiry_warnings(session, bot=bot))
    await _step("traffic_warnings", send_traffic_warnings(session, bot=bot))
    return out


# --------------------------------------------------------------------------
# Messaging
# --------------------------------------------------------------------------
def build_action_message(order, product, service: V2RayService, action: str, lang: str = "fa") -> str:
    """Delivery message for a completed renew/add-traffic action."""
    from app.i18n import t
    expire = service.expire_at.strftime("%Y-%m-%d") if service.expire_at else "—"
    total = int(service.total_gb or 0)
    total_gb = "∞" if total <= 0 else f"{total / (1024 ** 3):.0f}"
    key = "service.renew.done" if action == "renew_service" else "service.addtraffic.done"
    lines = [
        t(key, lang),
        "",
        t("service.delivery.order", lang, number=order.order_number),
        t("service.delivery.product", lang, title=product.title if product else "—"),
        t("service.delivery.expire", lang, date=expire),
        t("service.action.total_traffic", lang, gb=total_gb),
    ]
    if service.subscription_url:
        lines += ["", t("service.delivery.sub_link", lang),
                  f"<code>{service.subscription_url}</code>"]
    return "\n".join(lines)

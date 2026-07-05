"""Admin-facing management of 3X-UI servers and their inbounds (foundation).

This is the panel-side CRUD layer: it stores server records (encrypting the
panel password/token at rest via app/core/crypto.py) and inbound records, and
audit-logs every change WITHOUT ever putting a credential in the audit metadata.

Live connectivity (test_connection / sync_inbounds) delegates to the low-level
client in app/services/xui_service.py; if a panel is unreachable it records the
error on the server rather than raising, so product binding never depends on a
live sync.
"""
from __future__ import annotations

import logging
from datetime import datetime, timezone

from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.core import crypto
from app.models.xui_inbound import XuiInbound
from app.models.xui_server import XuiServer
from app.services import audit_service

log = logging.getLogger("xui.server_service")


def _now() -> datetime:
    return datetime.now(timezone.utc)


# --------------------------------------------------------------------------
# Servers
# --------------------------------------------------------------------------
async def create_server(
    session: AsyncSession,
    *,
    name: str,
    base_url: str,
    username: str | None = None,
    password: str | None = None,
    api_token: str | None = None,
    is_active: bool = True,
    actor_id: int | None = None,
) -> XuiServer:
    if not (name or "").strip() or not (base_url or "").strip():
        raise ValueError("name and base_url are required")
    server = XuiServer(
        name=name.strip(),
        base_url=base_url.strip().rstrip("/"),
        username=(username or "").strip() or None,
        encrypted_password=crypto.encrypt(password) if password else None,
        encrypted_api_token=crypto.encrypt(api_token) if api_token else None,
        status="unknown",
        is_active=is_active,
    )
    session.add(server)
    await session.flush()
    await audit_service.log(
        session, actor_type="admin", actor_id=actor_id,
        action="xui_server_created", target_type="xui_server", target_id=server.id,
        new=f"name={server.name!r}",  # never credentials
    )
    await session.refresh(server)
    return server


async def update_server(
    session: AsyncSession,
    server_id: int,
    *,
    name: str | None = None,
    base_url: str | None = None,
    username: str | None = None,
    password: str | None = None,
    api_token: str | None = None,
    is_active: bool | None = None,
    actor_id: int | None = None,
) -> XuiServer | None:
    """Update a server. An empty/None password (or token) keeps the stored one."""
    server = await get_server(session, server_id)
    if server is None:
        return None
    if name is not None:
        server.name = name.strip()
    if base_url is not None:
        server.base_url = base_url.strip().rstrip("/")
    if username is not None:
        server.username = username.strip() or None
    if password:  # only overwrite when a new non-empty password is supplied
        server.encrypted_password = crypto.encrypt(password)
    if api_token:
        server.encrypted_api_token = crypto.encrypt(api_token)
    if is_active is not None:
        server.is_active = is_active
    await audit_service.log(
        session, actor_type="admin", actor_id=actor_id,
        action="xui_server_updated", target_type="xui_server", target_id=server.id,
    )
    await session.refresh(server)
    return server


async def delete_or_deactivate_server(
    session: AsyncSession, server_id: int, *, actor_id: int | None = None
) -> XuiServer | None:
    """Deactivate a server (soft) so any product bindings survive."""
    server = await get_server(session, server_id)
    if server is None:
        return None
    server.is_active = False
    server.status = "inactive"
    await audit_service.log(
        session, actor_type="admin", actor_id=actor_id,
        action="xui_server_deactivated", target_type="xui_server", target_id=server.id,
    )
    await session.refresh(server)
    return server


async def list_servers(session: AsyncSession, *, active_only: bool = False) -> list[XuiServer]:
    stmt = select(XuiServer).order_by(XuiServer.id)
    if active_only:
        stmt = stmt.where(XuiServer.is_active.is_(True))
    return list((await session.execute(stmt)).scalars().all())


async def get_server(session: AsyncSession, server_id: int) -> XuiServer | None:
    return await session.get(XuiServer, server_id)


async def inbound_counts(session: AsyncSession) -> dict[int, int]:
    result = await session.execute(
        select(XuiInbound.server_id, func.count(XuiInbound.id)).group_by(XuiInbound.server_id)
    )
    return {sid: c for sid, c in result.all()}


async def set_server_status(
    session: AsyncSession, server_id: int, status: str, *, last_error: str | None = None
) -> XuiServer | None:
    server = await get_server(session, server_id)
    if server is None:
        return None
    server.status = status
    server.last_error = last_error
    server.last_health_check = _now()
    await session.commit()
    return server


# --------------------------------------------------------------------------
# Inbounds
# --------------------------------------------------------------------------
async def create_inbound(
    session: AsyncSession,
    server_id: int,
    inbound_id: int,
    *,
    remark: str | None = None,
    protocol: str | None = None,
    port: int | None = None,
    network: str | None = None,
    security: str | None = None,
    is_active: bool = True,
    actor_id: int | None = None,
) -> XuiInbound:
    if await get_server(session, server_id) is None:
        raise ValueError("server not found")
    inbound = XuiInbound(
        server_id=server_id,
        inbound_id=int(inbound_id),
        remark=remark,
        protocol=protocol,
        port=port,
        network=network,
        security=security,
        is_active=is_active,
    )
    session.add(inbound)
    await session.flush()
    await audit_service.log(
        session, actor_type="admin", actor_id=actor_id,
        action="xui_inbound_created", target_type="xui_inbound", target_id=inbound.id,
        new=f"server_id={server_id} inbound_id={inbound_id}",
    )
    await session.refresh(inbound)
    return inbound


async def get_inbound(session: AsyncSession, inbound_record_id: int) -> XuiInbound | None:
    return await session.get(XuiInbound, inbound_record_id)


async def update_inbound(
    session: AsyncSession,
    inbound_record_id: int,
    *,
    inbound_id: int | None = None,
    remark: str | None = None,
    protocol: str | None = None,
    port: int | None = None,
    network: str | None = None,
    security: str | None = None,
    is_active: bool | None = None,
    actor_id: int | None = None,
) -> XuiInbound | None:
    inbound = await get_inbound(session, inbound_record_id)
    if inbound is None:
        return None
    if inbound_id is not None:
        inbound.inbound_id = int(inbound_id)
    if remark is not None:
        inbound.remark = remark
    if protocol is not None:
        inbound.protocol = protocol
    if port is not None:
        inbound.port = port
    if network is not None:
        inbound.network = network
    if security is not None:
        inbound.security = security
    if is_active is not None:
        inbound.is_active = is_active
    await audit_service.log(
        session, actor_type="admin", actor_id=actor_id,
        action="xui_inbound_updated", target_type="xui_inbound", target_id=inbound.id,
    )
    await session.refresh(inbound)
    return inbound


async def list_inbounds(
    session: AsyncSession, server_id: int, *, active_only: bool = False
) -> list[XuiInbound]:
    stmt = select(XuiInbound).where(XuiInbound.server_id == server_id).order_by(XuiInbound.inbound_id)
    if active_only:
        stmt = stmt.where(XuiInbound.is_active.is_(True))
    return list((await session.execute(stmt)).scalars().all())


async def deactivate_inbound(
    session: AsyncSession, inbound_record_id: int, *, actor_id: int | None = None
) -> XuiInbound | None:
    inbound = await get_inbound(session, inbound_record_id)
    if inbound is None:
        return None
    inbound.is_active = False
    await audit_service.log(
        session, actor_type="admin", actor_id=actor_id,
        action="xui_inbound_deactivated", target_type="xui_inbound", target_id=inbound.id,
    )
    await session.refresh(inbound)
    return inbound


# --------------------------------------------------------------------------
# Live connectivity (best-effort; never blocks product binding)
# --------------------------------------------------------------------------
async def test_connection(
    session: AsyncSession, server_id: int, *, actor_id: int | None = None, transport=None
) -> dict[str, object]:
    """Log in to the panel; record status=active on success, error otherwise."""
    from app.services import xui_service  # local import keeps httpx off the import path
    from app.xui.exceptions import XuiError

    server = await get_server(session, server_id)
    if server is None:
        return {"ok": False, "status": "unknown", "message": "server not found"}

    adapter = xui_service.build_adapter(server, transport=transport)
    try:
        await adapter.login()
        status, ok, message = "active", True, "connected"
    except XuiError as exc:
        status, ok, message = "error", False, str(exc)
    finally:
        await adapter.aclose()

    server.status = status
    server.last_error = None if ok else message
    server.last_health_check = _now()
    await audit_service.log(
        session, actor_type="admin", actor_id=actor_id,
        action="xui_server_tested", target_type="xui_server", target_id=server.id,
        new=f"status={status}",  # no credentials
    )
    return {"ok": ok, "status": status, "message": message}


async def sync_inbounds(
    session: AsyncSession, server_id: int, *, actor_id: int | None = None, transport=None
) -> dict[str, object]:
    """Pull inbounds from the panel into XuiInbound rows (idempotent upsert)."""
    from app.services import xui_service
    from app.xui.exceptions import XuiError

    server = await get_server(session, server_id)
    if server is None:
        return {"ok": False, "count": 0, "message": "server not found"}
    try:
        count = await xui_service.sync_inbounds(session, server, transport=transport)
        server.status = "active"
        server.last_error = None
    except XuiError as exc:
        server.status = "error"
        server.last_error = str(exc)
        await session.commit()
        return {"ok": False, "count": 0, "message": str(exc)}
    await audit_service.log(
        session, actor_type="admin", actor_id=actor_id,
        action="xui_inbounds_synced", target_type="xui_server", target_id=server.id,
        new=f"count={count}",
    )
    return {"ok": True, "count": count, "message": f"synced {count} inbound(s)"}

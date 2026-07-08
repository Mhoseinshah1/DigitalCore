"""3X-UI service: the ONLY module the rest of the app uses to reach a panel.

Resolves a version-selectable adapter (app/xui/registry.py), stores server
credentials encrypted at rest (app/core/crypto.py), and never logs secrets.
Client-write operations expose a verify-after-write path for a later phase.
"""
from __future__ import annotations

import logging
from datetime import datetime, timezone

import httpx
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.core import crypto
from app.services import audit_service
from app.models.xui_inbound import XuiInbound
from app.models.xui_server import XuiServer
from app.xui.base import PanelAdapter
from app.xui.client import SleepFn
from app.xui.exceptions import XuiAuthError, XuiError, XuiVerificationError
from app.xui.registry import SUPPORTED_VERSIONS, get_adapter
from app.xui.schemas import Client, ClientAdd, ClientTraffic, ClientUpdate

log = logging.getLogger("xui.service")


def _now() -> datetime:
    return datetime.now(timezone.utc)


def build_adapter(
    server: XuiServer,
    *,
    transport: httpx.BaseTransport | None = None,
    sleep: SleepFn | None = None,
) -> PanelAdapter:
    """Build a decrypted, ready adapter for a server (test injection points)."""
    return get_adapter(server, transport=transport, sleep=sleep)


# --------------------------------------------------------------------------
# Server management
# --------------------------------------------------------------------------
async def add_server(
    session: AsyncSession,
    *,
    name: str,
    base_url: str,
    username: str,
    password: str,
    web_base_path: str | None = None,
    panel_type: str = "3x-ui",
    panel_version: str = "2.9.4",
    api_token: str | None = None,
    actor_type: str = "system",
    actor_id: int | None = None,
) -> XuiServer:
    if panel_version not in SUPPORTED_VERSIONS:
        raise ValueError(f"unsupported panel_version {panel_version!r}")
    if not (name or "").strip() or not (base_url or "").strip():
        raise ValueError("name and base_url are required")

    server = XuiServer(
        name=name.strip(),
        base_url=base_url.strip().rstrip("/"),
        web_base_path=(web_base_path or "").strip() or None,
        panel_type=panel_type,
        panel_version=panel_version,
        username=username,
        encrypted_password=crypto.encrypt(password),
        encrypted_api_token=crypto.encrypt(api_token) if api_token else None,
        status="unknown",
    )
    session.add(server)
    await session.flush()
    await audit_service.log(
        session,
        actor_type=actor_type,
        actor_id=actor_id,
        action="xui_server.created",
        target_type="xui_server",
        target_id=server.id,
        new=f"name={server.name!r} version={server.panel_version}",  # no secrets
    )
    await session.commit()
    await session.refresh(server)
    return server


async def list_servers(session: AsyncSession) -> list[XuiServer]:
    result = await session.execute(select(XuiServer).order_by(XuiServer.id))
    return list(result.scalars().all())


async def get_server(session: AsyncSession, server_id: int) -> XuiServer | None:
    return await session.get(XuiServer, server_id)


async def delete_server(
    session: AsyncSession,
    server_id: int,
    *,
    actor_type: str = "system",
    actor_id: int | None = None,
) -> bool:
    server = await session.get(XuiServer, server_id)
    if server is None:
        return False
    await session.delete(server)
    await audit_service.log(
        session,
        actor_type=actor_type,
        actor_id=actor_id,
        action="xui_server.deleted",
        target_type="xui_server",
        target_id=server_id,
    )
    await session.commit()
    return True


# --------------------------------------------------------------------------
# Connectivity + sync
# --------------------------------------------------------------------------
def _normalize_inbound(raw: dict) -> dict:
    """Panel inbound JSON → the fields we persist (network/security from stream)."""
    import json as _json
    network = security = None
    stream = raw.get("streamSettings")
    if stream:
        try:
            s = _json.loads(stream) if isinstance(stream, str) else stream
            network = s.get("network")
            security = s.get("security")
        except (ValueError, TypeError, AttributeError):
            pass
    return {
        "remote_inbound_id": int(raw.get("id", 0) or 0),
        "remark": (raw.get("remark") or None),
        "protocol": (raw.get("protocol") or None),
        "port": int(raw.get("port", 0) or 0) or None,
        "network": network,
        "security": security,
        "tag": (raw.get("tag") or None),
        "enable": bool(raw.get("enable", True)),
        "raw_json": _json.dumps(raw)[:20000],
    }


async def test_connection(
    session: AsyncSession,
    server: XuiServer,
    *,
    transport: httpx.BaseTransport | None = None,
    sleep: SleepFn | None = None,
) -> dict[str, object]:
    """Rich connection test via the Sanaei API client (auth + status + inbounds).

    Records the outcome (status, last error, panel/xray version) on the server.
    Returns a secret-free diagnostic dict for the admin UI.
    """
    from app.services.sanaei_adapter import build_client

    client = build_client(server, transport=transport, sleep=sleep)
    try:
        diag = await client.test_connection()
    finally:
        await client.aclose()

    if diag["ok"]:
        server.status = "online"
        server.last_error = None
    else:
        err = str(diag.get("error") or "")
        server.status = "auth_error" if err.startswith("auth:") else "offline"
        server.last_error = err or "connection failed"
    if diag.get("panel_version"):
        server.panel_version = str(diag["panel_version"])[:32]
    if diag.get("xray_version"):
        server.xray_version = str(diag["xray_version"])[:32]
    server.last_health_check = _now()
    await session.commit()

    return {
        "ok": bool(diag["ok"]),
        "status": server.status,
        "message": ("connected" if diag["ok"] else (server.last_error or "failed")),
        "auth_mode": diag.get("auth_mode"),
        "inbound_count": diag.get("inbound_count"),
        "panel_version": diag.get("panel_version"),
        "xray_version": diag.get("xray_version"),
        "server_status_ok": diag.get("server_status_ok"),
    }


async def sync_inbounds(
    session: AsyncSession,
    server: XuiServer,
    *,
    transport: httpx.BaseTransport | None = None,
    sleep: SleepFn | None = None,
) -> int:
    """Pull inbounds from the panel and upsert XuiInbound rows (idempotent).

    Stores protocol/port/network/security/tag/raw_json and marks synced_at.
    Missing inbounds are NOT deleted — they are left as-is (admin decides)."""
    from app.services.sanaei_adapter import build_client

    client = build_client(server, transport=transport, sleep=sleep)
    try:
        raw_inbounds = await client.list_inbounds()
    finally:
        await client.aclose()

    existing = {
        row.inbound_id: row
        for row in (
            await session.execute(
                select(XuiInbound).where(XuiInbound.server_id == server.id)
            )
        ).scalars()
    }
    for raw in raw_inbounds:
        if not isinstance(raw, dict):
            continue
        norm = _normalize_inbound(raw)
        rid = norm["remote_inbound_id"]
        if not rid:
            continue
        row = existing.get(rid)
        if row is None:
            # New row: seed is_active from the panel's enable. Existing rows keep
            # their admin-set is_active (only the panel mirror is refreshed).
            row = XuiInbound(server_id=server.id, inbound_id=rid, is_active=norm["enable"])
            session.add(row)
        row.remark = norm["remark"]
        row.protocol = norm["protocol"]
        row.port = norm["port"]
        row.network = norm["network"]
        row.security = norm["security"]
        row.tag = norm["tag"]
        row.enable_from_panel = norm["enable"]
        row.raw_json = norm["raw_json"]
        row.synced_at = _now()

    server.status = "online"
    server.last_health_check = _now()
    await session.commit()
    return len([r for r in raw_inbounds if isinstance(r, dict)])


# --------------------------------------------------------------------------
# Client operations (implemented; NOT wired into order approval yet)
# --------------------------------------------------------------------------
def _matches(actual: Client, expected: ClientAdd | ClientUpdate) -> bool:
    return (
        actual.email == expected.email
        and actual.enable == expected.enable
        and actual.expiry_time == expected.expiry_time
        and actual.total_gb == expected.total_gb
        and actual.limit_ip == expected.limit_ip
    )


async def verify_client(
    adapter: PanelAdapter,
    inbound_id: int,
    expected: ClientAdd | ClientUpdate,
) -> Client:
    """Read the client back from the panel and confirm it matches `expected`.

    Raises XuiVerificationError on a missing client or a field mismatch.
    Exposed for a later provisioning phase to guarantee writes landed.
    """
    actual = await adapter.find_client(inbound_id, expected.email)
    if actual is None:
        raise XuiVerificationError(f"client {expected.email!r} not found after write")
    if not _matches(actual, expected):
        raise XuiVerificationError(
            f"client {expected.email!r} on the panel does not match the written values"
        )
    return actual


async def find_client(
    server: XuiServer,
    inbound_id: int,
    email: str,
    *,
    adapter: PanelAdapter | None = None,
    transport: httpx.BaseTransport | None = None,
    sleep: SleepFn | None = None,
) -> Client | None:
    """Return the client with `email` on the inbound, or None. Used for
    idempotent provisioning (detect a client left by a prior partial run)."""
    a = adapter or build_adapter(server, transport=transport, sleep=sleep)
    try:
        await a.login()
        return await a.find_client(inbound_id, email)
    finally:
        if adapter is None:
            await a.aclose()


async def add_client(
    server: XuiServer,
    inbound_id: int,
    client: ClientAdd,
    *,
    verify: bool = True,
    adapter: PanelAdapter | None = None,
    transport: httpx.BaseTransport | None = None,
    sleep: SleepFn | None = None,
) -> Client | None:
    a = adapter or build_adapter(server, transport=transport, sleep=sleep)
    try:
        await a.login()
        await a.add_client(inbound_id, client)
        if verify:
            return await verify_client(a, inbound_id, client)
        return None
    finally:
        if adapter is None:
            await a.aclose()


async def update_client(
    server: XuiServer,
    inbound_id: int,
    client: ClientUpdate,
    *,
    verify: bool = True,
    adapter: PanelAdapter | None = None,
    transport: httpx.BaseTransport | None = None,
    sleep: SleepFn | None = None,
) -> Client | None:
    a = adapter or build_adapter(server, transport=transport, sleep=sleep)
    try:
        await a.login()
        await a.update_client(inbound_id, client)
        if verify:
            return await verify_client(a, inbound_id, client)
        return None
    finally:
        if adapter is None:
            await a.aclose()


async def delete_client(
    server: XuiServer,
    inbound_id: int,
    client_uuid_or_email: str,
    *,
    adapter: PanelAdapter | None = None,
    transport: httpx.BaseTransport | None = None,
    sleep: SleepFn | None = None,
) -> None:
    a = adapter or build_adapter(server, transport=transport, sleep=sleep)
    try:
        await a.login()
        await a.delete_client(inbound_id, client_uuid_or_email)
    finally:
        if adapter is None:
            await a.aclose()


async def set_client_enabled(
    server: XuiServer,
    inbound_id: int,
    client: ClientUpdate,
    enabled: bool,
    *,
    adapter: PanelAdapter | None = None,
    transport: httpx.BaseTransport | None = None,
    sleep: SleepFn | None = None,
) -> None:
    a = adapter or build_adapter(server, transport=transport, sleep=sleep)
    try:
        await a.login()
        await a.set_client_enabled(inbound_id, client, enabled)
    finally:
        if adapter is None:
            await a.aclose()


async def reset_client_traffic(
    server: XuiServer,
    inbound_id: int,
    email: str,
    *,
    adapter: PanelAdapter | None = None,
    transport: httpx.BaseTransport | None = None,
    sleep: SleepFn | None = None,
) -> None:
    a = adapter or build_adapter(server, transport=transport, sleep=sleep)
    try:
        await a.login()
        await a.reset_client_traffic(inbound_id, email)
    finally:
        if adapter is None:
            await a.aclose()


async def get_client_traffic(
    server: XuiServer,
    email: str,
    *,
    adapter: PanelAdapter | None = None,
    transport: httpx.BaseTransport | None = None,
    sleep: SleepFn | None = None,
) -> ClientTraffic:
    a = adapter or build_adapter(server, transport=transport, sleep=sleep)
    try:
        await a.login()
        return await a.get_client_traffic(email)
    finally:
        if adapter is None:
            await a.aclose()

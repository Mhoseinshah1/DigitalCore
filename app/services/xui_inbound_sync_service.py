"""Automatic 3X-UI / Sanaei inbound discovery + sync.

Owner requirement: admins never type inbound IDs. Adding a server and syncing
pulls **every** inbound from the panel and upserts it locally (by
``server_id`` + remote inbound id); product binding then only ever picks from
the synced, active inbounds. This module is the single, centralised place that
sync happens — the web panel, the bot, and the worker all call it.

Everything here is best-effort and never raises to the caller: a panel/auth/
network failure is captured into :class:`SyncResult.error_message` (secret-free)
and logged, so a broken server can never break a bot flow or a web request.
"""
from __future__ import annotations

import json
import logging
from dataclasses import dataclass
from datetime import datetime, timezone

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.models.xui_inbound import XuiInbound
from app.models.xui_server import XuiServer

log = logging.getLogger("xui.sync")

# Protocols a 3X-UI / Sanaei / Xray panel can expose. We store whatever the panel
# reports; this set is only used to lower-case-normalise and document coverage.
KNOWN_PROTOCOLS: frozenset[str] = frozenset({
    "vless", "vmess", "trojan", "shadowsocks", "socks", "http", "mixed",
    "dokodemo-door", "wireguard", "hysteria", "hysteria2", "tuic",
})


@dataclass
class SyncResult:
    """Outcome of syncing one server's inbounds — safe to show an admin."""

    server_id: int
    server_name: str
    success: bool
    created_count: int = 0
    updated_count: int = 0
    disabled_count: int = 0
    total_remote_count: int = 0
    error_message: str | None = None
    synced_at: datetime | None = None


def _now() -> datetime:
    return datetime.now(timezone.utc)


def normalize_inbound(raw: object) -> dict:
    """Panel inbound JSON → the fields we persist.

    Tolerates ``streamSettings``/``settings`` being a JSON string, an object, or
    absent, and never raises on malformed input (returns an empty ``dict`` when
    the row is not a usable inbound). ``network``/``security`` come from
    ``streamSettings``; ``protocol`` is lower-cased.
    """
    if not isinstance(raw, dict):
        return {}
    network = security = None
    stream = raw.get("streamSettings")
    if stream:
        try:
            parsed = json.loads(stream) if isinstance(stream, str) else stream
            if isinstance(parsed, dict):
                network = parsed.get("network")
                security = parsed.get("security")
        except (ValueError, TypeError):
            pass
    try:
        remote_id = int(raw.get("id", 0) or 0)
    except (ValueError, TypeError):
        remote_id = 0
    try:
        port = int(raw.get("port", 0) or 0) or None
    except (ValueError, TypeError):
        port = None
    protocol = raw.get("protocol")
    return {
        "remote_inbound_id": remote_id,
        "remark": (raw.get("remark") or None),
        "protocol": (str(protocol).strip().lower() or None) if protocol else None,
        "port": port,
        "network": (str(network) if network else None),
        "security": (str(security) if security else None),
        "tag": (raw.get("tag") or None),
        "enable": bool(raw.get("enable", True)),
        "raw_json": json.dumps(raw, ensure_ascii=False)[:20000],
    }


async def _existing_by_remote_id(session: AsyncSession, server_id: int) -> dict[int, XuiInbound]:
    rows = (await session.execute(
        select(XuiInbound).where(XuiInbound.server_id == server_id))).scalars()
    return {row.inbound_id: row for row in rows}


async def sync_server_inbounds(
    session: AsyncSession, server_id: int, *, transport=None,
    mark_missing_inactive: bool = True,
) -> SyncResult:
    """Discover + upsert all inbounds of one server. Returns a :class:`SyncResult`.

    * Upserts by (server_id, remote inbound id); never deletes.
    * ``mark_missing_inactive`` (default True): an inbound that the panel no
      longer returns is marked ``is_active=False`` locally (kept for history) so
      it can't be sold — the remote inbound is never touched.
    * New rows seed ``is_active`` from the panel's ``enable``; existing rows keep
      their admin-set ``is_active`` (only the panel mirror + details refresh).
    """
    from app.services.sanaei_adapter import build_client
    from app.xui.exceptions import XuiError

    server = await session.get(XuiServer, server_id)
    if server is None:
        return SyncResult(server_id=server_id, server_name="?", success=False,
                          error_message="server not found")
    result = SyncResult(server_id=server.id, server_name=server.name, success=False)

    client = build_client(server, transport=transport)
    try:
        raw_list = await client.list_inbounds()
    except XuiError as exc:
        server.status = "error"
        server.last_error = str(exc)[:500]
        await session.commit()
        result.error_message = str(exc)[:500]
        log.warning("inbound sync failed for server %s: %s", server.id, type(exc).__name__)
        return result
    except Exception as exc:  # noqa: BLE001 - a broken panel must never crash the caller
        server.status = "error"
        server.last_error = "sync failed"
        await session.commit()
        result.error_message = "sync failed"
        log.warning("unexpected inbound sync error for server %s: %s", server.id, type(exc).__name__)
        return result
    finally:
        await client.aclose()

    existing = await _existing_by_remote_id(session, server.id)
    seen: set[int] = set()
    for raw in raw_list:
        norm = normalize_inbound(raw)
        rid = norm.get("remote_inbound_id")
        if not rid:
            continue
        seen.add(rid)
        row = existing.get(rid)
        if row is None:
            row = XuiInbound(server_id=server.id, inbound_id=rid, is_active=norm["enable"])
            session.add(row)
            result.created_count += 1
        else:
            result.updated_count += 1
        row.remark = norm["remark"]
        row.protocol = norm["protocol"]
        row.port = norm["port"]
        row.network = norm["network"]
        row.security = norm["security"]
        row.tag = norm["tag"]
        row.enable_from_panel = norm["enable"]
        row.raw_json = norm["raw_json"]
        row.synced_at = _now()

    if mark_missing_inactive:
        for rid, row in existing.items():
            if rid not in seen and row.is_active:
                row.is_active = False
                result.disabled_count += 1

    server.status = "active"
    server.last_error = None
    server.last_health_check = _now()
    result.total_remote_count = len(seen)
    result.success = True
    result.synced_at = _now()
    await session.commit()
    log.info("synced server %s inbounds: created=%d updated=%d disabled=%d total=%d",
             server.id, result.created_count, result.updated_count,
             result.disabled_count, result.total_remote_count)
    return result


async def sync_all_active_servers(session: AsyncSession, *, transport=None) -> list[SyncResult]:
    """Sync every active server (worker / bulk diagnostic). Never raises."""
    servers = (await session.execute(
        select(XuiServer).where(XuiServer.is_active.is_(True)).order_by(XuiServer.id))).scalars().all()
    results: list[SyncResult] = []
    for server in servers:
        results.append(await sync_server_inbounds(session, server.id, transport=transport))
    return results


async def list_active_synced_inbounds(session: AsyncSession, server_id: int) -> list[XuiInbound]:
    """Active inbounds available for product binding (sorted by remote id)."""
    rows = (await session.execute(
        select(XuiInbound)
        .where(XuiInbound.server_id == server_id, XuiInbound.is_active.is_(True))
        .order_by(XuiInbound.inbound_id))).scalars().all()
    return list(rows)


async def get_best_default_inbound(session: AsyncSession, server_id: int) -> XuiInbound | None:
    """The inbound to auto-select for a product: the sole active inbound of a
    server, or None when there are zero or several (the admin must choose)."""
    active = await list_active_synced_inbounds(session, server_id)
    return active[0] if len(active) == 1 else None

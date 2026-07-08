"""Adapter that builds a :class:`SanaeiApiClient` from a stored ``XuiServer``.

This is the single place that decrypts a server's credentials and decides the
auth mode (API token preferred, username/password fallback). The rest of the app
talks to a panel exclusively through here, so endpoint knowledge and credential
handling stay centralized and secret-free.
"""
from __future__ import annotations

from typing import TYPE_CHECKING

import httpx

from app.services.sanaei_api_client import SanaeiApiClient, SleepFn

if TYPE_CHECKING:  # keep ORM/crypto out of import time
    from app.models.xui_server import XuiServer

# 3x-ui panels this integration is built and tested against.
SUPPORTED_PANEL_VERSIONS: tuple[str, ...] = ("2.9.4", "2.x", "latest")

DEFAULT_TIMEOUT = 20.0


def _server_timeout(server: "XuiServer") -> float:
    val = getattr(server, "timeout_seconds", None)
    try:
        return float(val) if val else DEFAULT_TIMEOUT
    except (TypeError, ValueError):
        return DEFAULT_TIMEOUT


def build_client(
    server: "XuiServer",
    *,
    transport: httpx.BaseTransport | None = None,
    sleep: SleepFn | None = None,
) -> SanaeiApiClient:
    """Build a ready :class:`SanaeiApiClient` for a stored server.

    API token is preferred; username/password are decrypted as the fallback.
    ``transport`` / ``sleep`` are test injection points (mock HTTP, no waits).
    """
    from app.core import crypto  # local import: keep this module import-light

    token = (
        crypto.decrypt(server.encrypted_api_token) if server.encrypted_api_token else None
    )
    password = (
        crypto.decrypt(server.encrypted_password) if server.encrypted_password else None
    )
    # Honour an explicit auth_mode when present, else prefer whatever is set.
    auth_mode = getattr(server, "auth_mode", None) or ("api_token" if token else "password")
    if auth_mode == "password":
        token = None  # force cookie login even if a stale token is stored

    kwargs: dict[str, object] = {
        "api_token": token,
        "username": server.username,
        "password": password,
        "web_base_path": server.web_base_path,
        "subscription_base_url": getattr(server, "public_sub_base_url", None),
        "subscription_path": getattr(server, "subscription_path", None),
        "tls_verify": bool(getattr(server, "tls_verify", True)),
        "timeout": _server_timeout(server),
        "transport": transport,
    }
    if sleep is not None:
        kwargs["sleep"] = sleep
    return SanaeiApiClient(server.base_url, **kwargs)  # type: ignore[arg-type]


class Sanaei3xuiAdapter:
    """Thin high-level façade over :class:`SanaeiApiClient` for a stored server.

    Kept intentionally small: it owns the client lifecycle and exposes the
    operations the service layer needs. Endpoint/version specifics live in the
    client (new ``/panel/api/clients/*`` first, legacy inbound flow on 404)."""

    def __init__(self, server: "XuiServer", *,
                 transport: httpx.BaseTransport | None = None,
                 sleep: SleepFn | None = None) -> None:
        self.server = server
        self.client = build_client(server, transport=transport, sleep=sleep)

    async def __aenter__(self) -> "Sanaei3xuiAdapter":
        return self

    async def __aexit__(self, *exc: object) -> None:
        await self.client.aclose()

    async def aclose(self) -> None:
        await self.client.aclose()

    # Delegations the service layer uses.
    async def test_connection(self) -> dict[str, object]:
        return await self.client.test_connection()

    async def list_inbounds(self) -> list[dict[str, object]]:
        return await self.client.list_inbounds()

    async def get_inbound(self, inbound_id: int) -> dict[str, object]:
        return await self.client.get_inbound(inbound_id)

    async def add_client(self, inbound_id: int, client: dict[str, object]) -> None:
        await self.client.add_client(inbound_id, client)

    async def get_client_by_email(self, inbound_id: int, email: str) -> dict[str, object] | None:
        return await self.client.get_client_by_email(inbound_id, email)

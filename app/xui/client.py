"""Low-level async HTTP session client for a 3X-UI panel.

Handles login (form POST, honouring a custom web_base_path), cookie-based
sessions with automatic re-login on 401 / expired session, and a request()
helper with a timeout and exponential-backoff retries on network/5xx errors.
Version-specific endpoint paths live in the adapters, not here.

The sleep function is injectable so tests exercise the backoff without waiting.
"""
from __future__ import annotations

import asyncio
import logging
from collections.abc import Awaitable, Callable
from typing import Any

import httpx

from app.xui.exceptions import (
    XuiApiError,
    XuiAuthError,
    XuiNetworkError,
    XuiNotFoundError,
)

log = logging.getLogger("xui.client")

SleepFn = Callable[[float], Awaitable[None]]


class XuiHttpClient:
    def __init__(
        self,
        *,
        base_url: str,
        username: str,
        password: str,
        web_base_path: str | None = None,
        timeout: float = 15.0,
        max_retries: int = 3,
        backoff_base: float = 0.5,
        transport: httpx.BaseTransport | None = None,
        sleep: SleepFn = asyncio.sleep,
    ) -> None:
        self._username = username
        self._password = password
        self._max_retries = max(1, max_retries)
        self._backoff_base = backoff_base
        self._sleep = sleep

        base = (base_url or "").rstrip("/")
        segment = (web_base_path or "").strip("/")
        self._base = f"{base}/{segment}" if segment else base

        self._client = httpx.AsyncClient(
            timeout=timeout, transport=transport, follow_redirects=False
        )
        self._logged_in = False

    @property
    def base(self) -> str:
        return self._base

    async def aclose(self) -> None:
        await self._client.aclose()

    async def __aenter__(self) -> "XuiHttpClient":
        return self

    async def __aexit__(self, *exc: object) -> None:
        await self.aclose()

    # -- login ---------------------------------------------------------------
    async def login(self) -> None:
        """Authenticate and store the session cookie. Raises XuiAuthError."""
        url = f"{self._base}/login"
        try:
            resp = await self._client.post(
                url, data={"username": self._username, "password": self._password}
            )
        except httpx.TransportError as exc:  # noqa: PERF203
            raise XuiNetworkError(f"login request failed: {exc}") from exc

        if resp.status_code != 200:
            raise XuiAuthError(f"login returned HTTP {resp.status_code}")
        try:
            payload = resp.json()
        except ValueError as exc:
            raise XuiAuthError("login response was not JSON") from exc
        if not (isinstance(payload, dict) and payload.get("success")):
            raise XuiAuthError(str(payload.get("msg")) if isinstance(payload, dict) else "login rejected")
        # httpx stores the Set-Cookie session into the client's cookie jar.
        self._logged_in = True

    # -- raw send with retry/backoff -----------------------------------------
    async def _send(
        self,
        method: str,
        url: str,
        *,
        data: dict[str, Any] | None = None,
        json: Any | None = None,
        params: dict[str, Any] | None = None,
    ) -> httpx.Response:
        last_exc: Exception | None = None
        for attempt in range(self._max_retries):
            try:
                resp = await self._client.request(
                    method, url, data=data, json=json, params=params
                )
            except httpx.TransportError as exc:
                last_exc = exc
                if attempt < self._max_retries - 1:
                    await self._sleep(self._backoff_base * (2**attempt))
                    continue
                raise XuiNetworkError(
                    f"network error after {self._max_retries} attempts: {exc}"
                ) from exc

            if resp.status_code >= 500:
                last_exc = XuiNetworkError(f"panel returned HTTP {resp.status_code}")
                if attempt < self._max_retries - 1:
                    await self._sleep(self._backoff_base * (2**attempt))
                    continue
                raise XuiNetworkError(
                    f"panel returned HTTP {resp.status_code} after {self._max_retries} attempts"
                )
            return resp
        # Unreachable, but keeps type checkers happy.
        raise XuiNetworkError(str(last_exc) if last_exc else "request failed")

    # -- request with envelope handling + auto re-login ----------------------
    async def request(
        self,
        method: str,
        path: str,
        *,
        data: dict[str, Any] | None = None,
        json: Any | None = None,
        params: dict[str, Any] | None = None,
        _allow_relogin: bool = True,
    ) -> Any:
        """Send an authenticated request and return the panel `obj` payload.

        Re-logs in once on a 401 / expired session. Raises the typed errors.
        """
        if not self._logged_in:
            await self.login()

        url = f"{self._base}{path}"
        resp = await self._send(method, url, data=data, json=json, params=params)

        if resp.status_code == 401:
            if _allow_relogin:
                self._logged_in = False
                await self.login()  # the one re-login attempt
                return await self.request(
                    method, path, data=data, json=json, params=params, _allow_relogin=False
                )
            raise XuiAuthError(f"{method} {path}: still unauthorized after re-login")

        if resp.status_code == 404:
            raise XuiNotFoundError(f"{method} {path}: not found")
        if resp.status_code != 200:
            raise XuiApiError(f"{method} {path}: HTTP {resp.status_code}")

        try:
            payload = resp.json()
        except ValueError:
            # A non-JSON body on a protected endpoint usually means the session
            # expired and the panel served its HTML login page.
            if _allow_relogin:
                self._logged_in = False
                await self.login()
                return await self.request(
                    method, path, data=data, json=json, params=params, _allow_relogin=False
                )
            raise XuiApiError(f"{method} {path}: non-JSON response body") from None

        if isinstance(payload, dict) and "success" in payload:
            if not payload.get("success"):
                raise XuiApiError(f"{method} {path}: {payload.get('msg') or 'panel reported failure'}")
            return payload.get("obj")
        return payload

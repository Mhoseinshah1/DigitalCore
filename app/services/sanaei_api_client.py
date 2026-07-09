"""Clean async client for the MHSanaei/3x-ui panel API.

Rebuilt against the Sanaei 3.x API contract. Design goals (in priority order):

1. **Bearer API token auth preferred** (``Authorization: Bearer <token>``). Cookie
   login (``POST {base}/login``) is a *fallback* used only when no token is set,
   or when a token request unexpectedly needs a session.
2. **Base-path aware**: the panel usually lives under a random ``web_base_path``
   (e.g. ``https://host:2053/AbC123/``). Every request is built off the normalized
   ``{base_url}/{web_base_path}`` root.
3. **TLS verify + timeout are per-server** and honoured on the httpx client.
4. **Structured errors** (reuses ``app.xui.exceptions``) so callers get a typed,
   user-mappable failure instead of a raw httpx error.
5. **Transient-failure retries** with capped exponential backoff.
6. **No secret leakage**: the token / password / cookies / raw bodies are never
   logged. Only method + redacted path + status code are logged.

The 3x-ui API wraps responses as ``{"success": bool, "msg": str, "obj": ...}``;
``_request`` unwraps ``obj`` and raises on ``success=false``. Key API quirks:

* ``expiryTime`` is epoch **milliseconds** (0 = never).
* ``totalGB`` is **bytes** despite the name (0 = unlimited).
* ``email`` is the unique client identifier.
* The panel generates ``id``/``password``/``subId`` server-side when omitted.
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

log = logging.getLogger("sanaei.api")

SleepFn = Callable[[float], Awaitable[None]]

# --- endpoint paths (relative to the normalized panel base) -----------------
PATH_LOGIN = "/login"
PATH_SERVER_STATUS = "/panel/api/server/status"
PATH_INBOUND_LIST = "/panel/api/inbounds/list"
# Some panel builds/versions expose the list at the collection root instead of
# /list; tried as a fallback when /list 404s. Both are centralised here.
PATH_INBOUND_LIST_ALT = "/panel/api/inbounds"
PATH_INBOUND_GET = "/panel/api/inbounds/get/{inbound_id}"
PATH_INBOUND_ALL_LINKS = "/panel/api/inbounds/allLinks"
# First-class client endpoints (newer API) — tried first.
PATH_CLIENT_ADD = "/panel/api/clients/add"
PATH_CLIENT_UPDATE = "/panel/api/clients/update/{email}"
PATH_CLIENT_DEL = "/panel/api/clients/del/{email}"
# Legacy inbound-scoped client endpoints — fallback when the above 404.
PATH_LEGACY_ADD_CLIENT = "/panel/api/inbounds/addClient"
PATH_LEGACY_UPDATE_CLIENT = "/panel/api/inbounds/updateClient/{client_id}"
PATH_LEGACY_DEL_CLIENT = "/panel/api/inbounds/{inbound_id}/delClient/{client}"
PATH_LEGACY_RESET_TRAFFIC = "/panel/api/inbounds/{inbound_id}/resetClientTraffic/{email}"
PATH_CLIENT_TRAFFIC = "/panel/api/inbounds/getClientTraffics/{email}"

_GB = 1024 ** 3


def normalize_base(base_url: str, web_base_path: str | None) -> str:
    """``{scheme}://host:port`` + optional ``/web_base_path`` — no trailing slash."""
    base = (base_url or "").strip().rstrip("/")
    segment = (web_base_path or "").strip().strip("/")
    return f"{base}/{segment}" if segment else base


class SanaeiApiClient:
    """One panel connection. Use as an async context manager."""

    def __init__(
        self,
        base_url: str,
        *,
        api_token: str | None = None,
        username: str | None = None,
        password: str | None = None,
        web_base_path: str | None = None,
        subscription_base_url: str | None = None,
        subscription_path: str | None = None,
        tls_verify: bool = True,
        timeout: float = 20.0,
        max_retries: int = 3,
        backoff_base: float = 0.5,
        transport: httpx.BaseTransport | None = None,
        sleep: SleepFn = asyncio.sleep,
    ) -> None:
        self._base = normalize_base(base_url, web_base_path)
        self._token = (api_token or "").strip() or None
        self._username = username
        self._password = password
        self._sub_base = (subscription_base_url or "").strip().rstrip("/") or None
        self._sub_path = (subscription_path or "/sub/") or "/sub/"
        self._max_retries = max(1, max_retries)
        self._backoff_base = backoff_base
        self._sleep = sleep
        self._logged_in = False
        self._client = httpx.AsyncClient(
            timeout=timeout, verify=tls_verify, transport=transport,
            follow_redirects=False,
        )

    # -- lifecycle -----------------------------------------------------------
    @property
    def base(self) -> str:
        return self._base

    @property
    def auth_mode(self) -> str:
        return "api_token" if self._token else "password"

    async def aclose(self) -> None:
        await self._client.aclose()

    async def __aenter__(self) -> "SanaeiApiClient":
        return self

    async def __aexit__(self, *exc: object) -> None:
        await self.aclose()

    # -- auth ----------------------------------------------------------------
    def _headers(self) -> dict[str, str]:
        headers = {"Accept": "application/json"}
        if self._token:
            headers["Authorization"] = f"Bearer {self._token}"
        return headers

    async def login(self) -> None:
        """Cookie login (fallback auth). Raises XuiAuthError / XuiNetworkError."""
        if not self._username:
            raise XuiAuthError("no API token and no username/password configured")
        url = f"{self._base}{PATH_LOGIN}"
        try:
            resp = await self._client.post(
                url, data={"username": self._username, "password": self._password or ""},
                headers={"Accept": "application/json"},
            )
        except httpx.TransportError as exc:
            raise XuiNetworkError(f"login request failed: {type(exc).__name__}") from exc
        if resp.status_code != 200:
            raise XuiAuthError(f"login returned HTTP {resp.status_code}")
        try:
            payload = resp.json()
        except ValueError as exc:
            raise XuiAuthError("login response was not JSON (wrong base path?)") from exc
        if not (isinstance(payload, dict) and payload.get("success")):
            msg = payload.get("msg") if isinstance(payload, dict) else "login rejected"
            raise XuiAuthError(str(msg or "login rejected"))
        self._logged_in = True

    async def ensure_auth(self) -> None:
        """Make sure the next request will authenticate. Token: nothing to do."""
        if self._token:
            return
        if not self._logged_in:
            await self.login()

    # -- transport with retry/backoff ---------------------------------------
    async def _send(self, method: str, url: str, **kw: Any) -> httpx.Response:
        last: Exception | None = None
        for attempt in range(self._max_retries):
            try:
                resp = await self._client.request(method, url, headers=self._headers(), **kw)
            except httpx.TransportError as exc:
                last = exc
                if attempt < self._max_retries - 1:
                    await self._sleep(self._backoff_base * (2 ** attempt))
                    continue
                raise XuiNetworkError(
                    f"network error after {self._max_retries} attempts: {type(exc).__name__}"
                ) from exc
            if resp.status_code >= 500:
                last = XuiNetworkError(f"panel HTTP {resp.status_code}")
                if attempt < self._max_retries - 1:
                    await self._sleep(self._backoff_base * (2 ** attempt))
                    continue
                raise XuiNetworkError(f"panel HTTP {resp.status_code} after retries")
            return resp
        raise XuiNetworkError(str(last) if last else "request failed")

    async def _request(
        self, method: str, path: str, *, data: Any = None, json: Any = None,
        params: dict[str, Any] | None = None, _relogin: bool = True,
    ) -> Any:
        """Authenticated request → unwrapped ``obj`` (or the raw payload).

        Never logs the body. Maps HTTP/panel errors to typed exceptions.
        """
        await self.ensure_auth()
        url = f"{self._base}{path}"
        resp = await self._send(method, url, data=data, json=json, params=params)
        log.debug("xui %s %s -> %s", method, _redact(path), resp.status_code)

        if resp.status_code == 401:
            if self._token:
                raise XuiAuthError(f"{method} {path}: API token rejected (401)")
            if _relogin:
                self._logged_in = False
                await self.login()
                return await self._request(method, path, data=data, json=json,
                                           params=params, _relogin=False)
            raise XuiAuthError(f"{method} {path}: unauthorized after re-login")
        if resp.status_code == 404:
            raise XuiNotFoundError(f"{method} {path}: not found (404)")
        if resp.status_code != 200:
            raise XuiApiError(f"{method} {path}: HTTP {resp.status_code}")

        try:
            payload = resp.json()
        except ValueError:
            # A protected endpoint returning HTML usually means an expired cookie
            # session (panel served its login page).
            if not self._token and _relogin:
                self._logged_in = False
                await self.login()
                return await self._request(method, path, data=data, json=json,
                                           params=params, _relogin=False)
            raise XuiApiError(f"{method} {path}: non-JSON response (auth/base-path?)") from None

        if isinstance(payload, dict) and "success" in payload:
            if not payload.get("success"):
                raise XuiApiError(f"{method} {path}: {payload.get('msg') or 'panel reported failure'}")
            return payload.get("obj")
        return payload

    # -- health / server -----------------------------------------------------
    async def get_server_status(self) -> dict[str, Any]:
        obj = await self._request("GET", PATH_SERVER_STATUS)
        return obj if isinstance(obj, dict) else {}

    async def test_connection(self) -> dict[str, Any]:
        """Probe auth + reachability. Returns a safe diagnostic summary dict.

        Never raises: every failure is captured into the returned dict so the
        admin UI can show a clear, secret-free reason.
        """
        result: dict[str, Any] = {
            "ok": False, "auth_mode": self.auth_mode, "base": self._base,
            "server_status_ok": False, "inbounds_ok": False,
            "inbound_count": None, "panel_version": None, "xray_version": None,
            "error": None,
        }
        try:
            await self.ensure_auth()
        except XuiAuthError as exc:
            result["error"] = f"auth: {exc}"
            return result
        except XuiNetworkError as exc:
            result["error"] = f"network: {exc}"
            return result

        # server status is best-effort (path varies by version); don't fail on it.
        try:
            status = await self.get_server_status()
            result["server_status_ok"] = True
            result["xray_version"] = _dig(status, "xray", "version") or status.get("xrayVersion")
            result["panel_version"] = status.get("version") or _dig(status, "appStats", "version")
        except XuiApiError:
            pass
        except (XuiNotFoundError, XuiNetworkError):
            pass

        # inbounds is the authoritative "auth works + API works" proof.
        try:
            inbounds = await self.list_inbounds()
            result["inbounds_ok"] = True
            result["inbound_count"] = len(inbounds)
            result["ok"] = True
        except XuiAuthError as exc:
            result["error"] = f"auth: {exc}"
        except XuiNotFoundError as exc:
            result["error"] = f"inbounds endpoint missing: {exc}"
        except (XuiApiError, XuiNetworkError) as exc:
            result["error"] = f"inbounds: {exc}"
        return result

    # -- inbounds ------------------------------------------------------------
    async def list_inbounds(self) -> list[dict[str, Any]]:
        """Every inbound on the panel, as raw dicts.

        Tries ``/panel/api/inbounds/list`` then falls back to
        ``/panel/api/inbounds`` for panel builds that expose it there. The
        ``{success, msg, obj}`` envelope is already unwrapped by ``_request``;
        the returned ``obj`` may itself be a list or a wrapper dict
        (``{"list": [...]}`` / ``{"inbounds": [...]}``), so we coerce safely.
        """
        try:
            obj = await self._request("GET", PATH_INBOUND_LIST)
        except XuiNotFoundError:
            obj = await self._request("GET", PATH_INBOUND_LIST_ALT)
        return _coerce_inbound_list(obj)

    async def get_inbound(self, inbound_id: int) -> dict[str, Any]:
        obj = await self._request("GET", PATH_INBOUND_GET.format(inbound_id=inbound_id))
        if not isinstance(obj, dict):
            raise XuiNotFoundError(f"inbound {inbound_id} not found")
        return obj

    async def get_all_links(self) -> Any:
        """Diagnostic fallback: every configured link across inbounds."""
        return await self._request("GET", PATH_INBOUND_ALL_LINKS)

    # -- clients (new API first, legacy fallback) ---------------------------
    async def add_client(self, inbound_id: int, client: dict[str, Any]) -> None:
        """Create a client. ``client`` uses panel field names (email/totalGB/
        expiryTime/enable/limitIp/tgId/subId/id...). totalGB is BYTES; expiryTime ms."""
        body = {"client": client, "inboundIds": [inbound_id]}
        try:
            await self._request("POST", PATH_CLIENT_ADD, json=body)
            return
        except XuiNotFoundError:
            pass  # newer endpoint absent → legacy inbound flow
        settings = _settings_str([client])
        await self._request("POST", PATH_LEGACY_ADD_CLIENT,
                            data={"id": inbound_id, "settings": settings})

    async def update_client(self, inbound_id: int, email: str, client: dict[str, Any]) -> None:
        """Replace a client's row. Provide the FULL set of fields to keep — the
        panel overwrites, it does not merge."""
        try:
            await self._request("POST", PATH_CLIENT_UPDATE.format(email=email),
                                json={"client": client, "inboundIds": [inbound_id]})
            return
        except XuiNotFoundError:
            pass
        client_id = client.get("id") or client.get("password") or email
        settings = _settings_str([client])
        await self._request("POST", PATH_LEGACY_UPDATE_CLIENT.format(client_id=client_id),
                            data={"id": inbound_id, "settings": settings})

    async def delete_client(self, inbound_id: int, email: str, *,
                            client_id: str | None = None, keep_traffic: bool = False) -> None:
        params = {"keepTraffic": 1 if keep_traffic else 0}
        try:
            await self._request("POST", PATH_CLIENT_DEL.format(email=email), params=params)
            return
        except XuiNotFoundError:
            pass
        await self._request("POST", PATH_LEGACY_DEL_CLIENT.format(
            inbound_id=inbound_id, client=client_id or email))

    async def enable_client(self, inbound_id: int, email: str, client: dict[str, Any]) -> None:
        await self.update_client(inbound_id, email, {**client, "enable": True})

    async def disable_client(self, inbound_id: int, email: str, client: dict[str, Any]) -> None:
        await self.update_client(inbound_id, email, {**client, "enable": False})

    async def reset_client_traffic(self, inbound_id: int, email: str) -> None:
        await self._request("POST", PATH_LEGACY_RESET_TRAFFIC.format(
            inbound_id=inbound_id, email=email))

    async def get_client_traffic(self, email: str) -> dict[str, Any]:
        obj = await self._request("GET", PATH_CLIENT_TRAFFIC.format(email=email))
        if obj is None:
            raise XuiNotFoundError(f"no traffic record for {email!r}")
        if isinstance(obj, list):
            if not obj:
                raise XuiNotFoundError(f"no traffic record for {email!r}")
            obj = obj[0]
        return obj if isinstance(obj, dict) else {}

    async def get_client_by_email(self, inbound_id: int, email: str) -> dict[str, Any] | None:
        """Find a client's full settings dict on an inbound (for full-payload updates)."""
        inbound = await self.get_inbound(inbound_id)
        for c in _clients_of(inbound):
            if str(c.get("email", "")) == email:
                return c
        return None

    # -- subscription URLs ---------------------------------------------------
    def build_subscription_url(self, sub_id: str) -> str | None:
        if not sub_id:
            return None
        base = self._sub_base or self._base
        path = self._sub_path if self._sub_path.startswith("/") else "/" + self._sub_path
        if not path.endswith("/"):
            path += "/"
        return f"{base.rstrip('/')}{path}{sub_id}"

    def build_json_subscription_url(self, sub_id: str) -> str | None:
        if not sub_id:
            return None
        base = self._sub_base or self._base
        return f"{base.rstrip('/')}/json/{sub_id}"

    def build_clash_subscription_url(self, sub_id: str) -> str | None:
        if not sub_id:
            return None
        base = self._sub_base or self._base
        return f"{base.rstrip('/')}/clash/{sub_id}"


# --------------------------------------------------------------------------
# helpers
# --------------------------------------------------------------------------
def _redact(path: str) -> str:
    """Drop an email/identifier tail so a client id never lands in logs."""
    return path.rsplit("/", 1)[0] + "/…" if path.count("/") > 3 else path


def _coerce_inbound_list(obj: Any) -> list[dict[str, Any]]:
    """Normalise an inbounds payload to a list of inbound dicts.

    Handles the response being a bare list, or a wrapper object that carries the
    list under a ``list``/``inbounds``/``obj``/``data`` key (varies by panel
    version). Non-dict entries are dropped so the caller never sees junk.
    """
    if isinstance(obj, list):
        return [x for x in obj if isinstance(x, dict)]
    if isinstance(obj, dict):
        for key in ("list", "inbounds", "obj", "data"):
            inner = obj.get(key)
            if isinstance(inner, list):
                return [x for x in inner if isinstance(x, dict)]
    return []


def _dig(d: Any, *keys: str) -> Any:
    for k in keys:
        if not isinstance(d, dict):
            return None
        d = d.get(k)
    return d


def _clients_of(inbound: dict[str, Any]) -> list[dict[str, Any]]:
    import json as _json
    raw = inbound.get("settings")
    if not raw:
        return []
    try:
        settings = _json.loads(raw) if isinstance(raw, str) else raw
        return list(settings.get("clients", []) or [])
    except (ValueError, TypeError, AttributeError):
        return []


def _settings_str(clients: list[dict[str, Any]]) -> str:
    import json as _json
    return _json.dumps({"clients": clients})


def bytes_for_gb(gb: float | int) -> int:
    return int(float(gb) * _GB)


def gb_for_bytes(byte_count: int) -> float:
    return round(int(byte_count or 0) / _GB, 2)

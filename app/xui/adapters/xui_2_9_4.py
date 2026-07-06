"""3X-UI (MHSanaei) v2.9.4 adapter.

ALL version-specific endpoint paths and JSON field names are kept local to this
class. The panel wraps responses as {success, msg, obj}; XuiHttpClient.request()
already unwraps `obj` and raises XuiApiError on success=false.
"""
from __future__ import annotations

import json
from typing import Any

from app.xui.base import PanelAdapter
from app.xui.exceptions import XuiApiError, XuiNotFoundError
from app.xui.schemas import Client, ClientAdd, ClientTraffic, ClientUpdate, Inbound

# --- version-specific endpoint paths (relative to the panel base) -----------
PATH_LIST = "/panel/api/inbounds/list"
PATH_GET = "/panel/api/inbounds/get/{inbound_id}"
PATH_ADD_CLIENT = "/panel/api/inbounds/addClient"
PATH_UPDATE_CLIENT = "/panel/api/inbounds/updateClient/{client_uuid}"
PATH_DEL_CLIENT = "/panel/api/inbounds/{inbound_id}/delClient/{client}"
# Reset path is inbound-id-first per the 3X-UI API: POST
# /panel/api/inbounds/{inboundId}/resetClientTraffic/{email}.
PATH_RESET_TRAFFIC = "/panel/api/inbounds/{inbound_id}/resetClientTraffic/{email}"
PATH_GET_TRAFFIC = "/panel/api/inbounds/getClientTraffics/{email}"


class Xui294Adapter(PanelAdapter):
    panel_type = "3x-ui"
    panel_version = "2.9.4"

    # -- parsing (panel JSON -> internal schema) -----------------------------
    @staticmethod
    def _parse_client(raw: dict[str, Any]) -> Client:
        return Client(
            email=str(raw.get("email", "")),
            uuid=raw.get("id") or raw.get("password"),
            enable=bool(raw.get("enable", True)),
            expiry_time=int(raw.get("expiryTime", 0) or 0),
            total_gb=int(raw.get("totalGB", 0) or 0),
            limit_ip=int(raw.get("limitIp", 0) or 0),
            sub_id=raw.get("subId"),
            tg_id=(str(raw["tgId"]) if raw.get("tgId") not in (None, "") else None),
            reset=int(raw.get("reset", 0) or 0),
        )

    @classmethod
    def _parse_inbound(cls, raw: dict[str, Any]) -> Inbound:
        clients: list[Client] = []
        settings_raw = raw.get("settings")
        if settings_raw:
            try:
                settings = json.loads(settings_raw) if isinstance(settings_raw, str) else settings_raw
                for c in settings.get("clients", []) or []:
                    clients.append(cls._parse_client(c))
            except (ValueError, TypeError, AttributeError):
                # Malformed settings JSON: expose the inbound with no clients
                # rather than failing the whole list. (Internal note, not user-facing.)
                clients = []
        return Inbound(
            inbound_id=int(raw.get("id", 0)),
            remark=str(raw.get("remark", "")),
            protocol=str(raw.get("protocol", "")),
            port=int(raw.get("port", 0) or 0),
            enable=bool(raw.get("enable", True)),
            clients=clients,
        )

    # -- serialising (internal schema -> panel JSON) -------------------------
    @staticmethod
    def _client_payload(client: ClientAdd | ClientUpdate) -> dict[str, Any]:
        payload: dict[str, Any] = {
            "id": client.uuid,
            "email": client.email,
            "enable": client.enable,
            "expiryTime": client.expiry_time,
            "totalGB": client.total_gb,
            "limitIp": client.limit_ip,
            "flow": "",
            "reset": 0,
        }
        if client.sub_id is not None:
            payload["subId"] = client.sub_id
        if client.tg_id is not None:
            payload["tgId"] = client.tg_id
        return payload

    @classmethod
    def _settings_string(cls, client: ClientAdd | ClientUpdate) -> str:
        return json.dumps({"clients": [cls._client_payload(client)]})

    # -- operations ----------------------------------------------------------
    async def login(self) -> None:
        await self.http.login()

    async def list_inbounds(self) -> list[Inbound]:
        obj = await self.http.request("GET", PATH_LIST)
        if not isinstance(obj, list):
            return []
        return [self._parse_inbound(item) for item in obj]

    async def get_inbound(self, inbound_id: int) -> Inbound:
        obj = await self.http.request("GET", PATH_GET.format(inbound_id=inbound_id))
        if not isinstance(obj, dict):
            raise XuiNotFoundError(f"inbound {inbound_id} not found")
        return self._parse_inbound(obj)

    async def add_client(self, inbound_id: int, client: ClientAdd) -> None:
        await self.http.request(
            "POST",
            PATH_ADD_CLIENT,
            data={"id": inbound_id, "settings": self._settings_string(client)},
        )

    async def update_client(self, inbound_id: int, client: ClientUpdate) -> None:
        if not client.uuid:
            raise XuiApiError("update_client requires the client uuid")
        await self.http.request(
            "POST",
            PATH_UPDATE_CLIENT.format(client_uuid=client.uuid),
            data={"id": inbound_id, "settings": self._settings_string(client)},
        )

    async def delete_client(self, inbound_id: int, client_uuid_or_email: str) -> None:
        await self.http.request(
            "POST",
            PATH_DEL_CLIENT.format(inbound_id=inbound_id, client=client_uuid_or_email),
        )

    async def set_client_enabled(
        self, inbound_id: int, client: ClientUpdate, enabled: bool
    ) -> None:
        updated = client.model_copy(update={"enable": enabled})
        await self.update_client(inbound_id, updated)

    async def reset_client_traffic(self, inbound_id: int, email: str) -> None:
        await self.http.request(
            "POST", PATH_RESET_TRAFFIC.format(inbound_id=inbound_id, email=email)
        )

    async def get_client_traffic(self, email: str) -> ClientTraffic:
        obj = await self.http.request("GET", PATH_GET_TRAFFIC.format(email=email))
        if obj is None:
            raise XuiNotFoundError(f"no traffic record for client {email!r}")
        if isinstance(obj, list):
            if not obj:
                raise XuiNotFoundError(f"no traffic record for client {email!r}")
            obj = obj[0]
        return ClientTraffic.model_validate(obj)

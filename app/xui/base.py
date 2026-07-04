"""Abstract PanelAdapter: the panel-agnostic operations the app relies on.

Every method is async and speaks the internal schemas from app.xui.schemas.
Concrete adapters (per panel type/version) translate to/from the panel's JSON.
"""
from __future__ import annotations

from abc import ABC, abstractmethod

from app.xui.client import XuiHttpClient
from app.xui.schemas import Client, ClientAdd, ClientTraffic, ClientUpdate, Inbound


class PanelAdapter(ABC):
    #: (panel_type, panel_version) this adapter serves — set by subclasses.
    panel_type: str = "3x-ui"
    panel_version: str = "2.9.4"

    def __init__(self, http: XuiHttpClient) -> None:
        self.http = http

    async def aclose(self) -> None:
        await self.http.aclose()

    @abstractmethod
    async def login(self) -> None:
        """Authenticate against the panel (raises XuiAuthError on failure)."""

    @abstractmethod
    async def list_inbounds(self) -> list[Inbound]:
        ...

    @abstractmethod
    async def get_inbound(self, inbound_id: int) -> Inbound:
        ...

    @abstractmethod
    async def add_client(self, inbound_id: int, client: ClientAdd) -> None:
        ...

    @abstractmethod
    async def update_client(self, inbound_id: int, client: ClientUpdate) -> None:
        ...

    @abstractmethod
    async def delete_client(self, inbound_id: int, client_uuid_or_email: str) -> None:
        ...

    @abstractmethod
    async def set_client_enabled(
        self, inbound_id: int, client: ClientUpdate, enabled: bool
    ) -> None:
        ...

    @abstractmethod
    async def reset_client_traffic(self, inbound_id: int, email: str) -> None:
        ...

    @abstractmethod
    async def get_client_traffic(self, email: str) -> ClientTraffic:
        ...

    async def find_client(self, inbound_id: int, email: str) -> Client | None:
        """Read an inbound and return the client with `email`, or None.

        Default implementation used by the verify-after-write helper; adapters
        may override for a cheaper lookup.
        """
        inbound = await self.get_inbound(inbound_id)
        for client in inbound.clients:
            if client.email == email:
                return client
        return None

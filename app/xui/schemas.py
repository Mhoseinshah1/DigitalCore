"""Panel-agnostic DTOs.

These internal shapes are what the rest of the app (and adapters) exchange.
Version-specific field names / payload quirks are translated inside each adapter
so nothing outside app/xui depends on a particular panel's JSON.
"""
from __future__ import annotations

from pydantic import BaseModel, ConfigDict, Field


class Client(BaseModel):
    """A single inbound client (VPN account)."""

    model_config = ConfigDict(populate_by_name=True)

    email: str
    uuid: str | None = None  # id/password depending on protocol
    enable: bool = True
    expiry_time: int = 0  # epoch millis; 0 = unlimited (panel convention)
    total_gb: int = 0  # bytes; 0 = unlimited (panel convention)
    limit_ip: int = 0
    sub_id: str | None = None
    tg_id: str | None = None
    reset: int = 0


class ClientTraffic(BaseModel):
    email: str
    enable: bool = True
    up: int = 0
    down: int = 0
    total: int = 0
    expiry_time: int = Field(default=0, alias="expiryTime")


class Inbound(BaseModel):
    """A panel inbound (listener). Clients are parsed out of its settings JSON."""

    inbound_id: int
    remark: str = ""
    protocol: str = ""
    port: int = 0
    enable: bool = True
    clients: list[Client] = Field(default_factory=list)


class ClientAdd(BaseModel):
    """Internal payload to create a client."""

    email: str
    uuid: str | None = None
    enable: bool = True
    expiry_time: int = 0
    total_gb: int = 0
    limit_ip: int = 0
    sub_id: str | None = None
    tg_id: str | None = None


class ClientUpdate(BaseModel):
    """Internal payload to update a client (must carry the client uuid)."""

    email: str
    uuid: str
    enable: bool = True
    expiry_time: int = 0
    total_gb: int = 0
    limit_ip: int = 0
    sub_id: str | None = None
    tg_id: str | None = None

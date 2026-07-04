"""Pydantic DTOs for products."""
from __future__ import annotations

from datetime import datetime

from pydantic import BaseModel, ConfigDict, Field


class ProductCreate(BaseModel):
    type: str
    title: str
    description: str | None = None
    price: int = Field(ge=0)
    duration_days: int | None = None
    traffic_gb: int | None = None
    ip_limit: int | None = None
    server_id: int | None = None
    inbound_id: int | None = None
    is_active: bool = True
    is_hidden: bool = False
    sort_order: int = 0


class ProductUpdate(BaseModel):
    """Partial update: only provided fields change."""

    type: str | None = None
    title: str | None = None
    description: str | None = None
    price: int | None = Field(default=None, ge=0)
    duration_days: int | None = None
    traffic_gb: int | None = None
    ip_limit: int | None = None
    server_id: int | None = None
    inbound_id: int | None = None
    is_active: bool | None = None
    is_hidden: bool | None = None
    sort_order: int | None = None


class ProductRead(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    id: int
    type: str
    title: str
    description: str | None
    price: int
    duration_days: int | None
    traffic_gb: int | None
    ip_limit: int | None
    server_id: int | None
    inbound_id: int | None
    is_active: bool
    is_hidden: bool
    stock_count: int
    sort_order: int
    created_at: datetime

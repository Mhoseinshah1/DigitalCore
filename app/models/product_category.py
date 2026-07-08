"""Product categories (bot UX): group products so the bot shows categories first.

A category is a simple, admin-managed grouping. Products link to at most one
category via ``Product.category_id`` (nullable); uncategorised products are shown
under a synthetic "سایر محصولات" (Other) group by the bot, so no data migration
of existing products is required.
"""
from __future__ import annotations

from sqlalchemy import Boolean, Integer, String, Text
from sqlalchemy.orm import Mapped, mapped_column

from app.models.base import Base, TimestampMixin


class ProductCategory(Base, TimestampMixin):
    __tablename__ = "product_categories"

    id: Mapped[int] = mapped_column(primary_key=True)
    title: Mapped[str] = mapped_column(String(200), nullable=False)
    slug: Mapped[str] = mapped_column(String(200), nullable=False, unique=True, index=True)
    description: Mapped[str | None] = mapped_column(Text, nullable=True)
    sort_order: Mapped[int] = mapped_column(Integer, default=0, nullable=False)
    is_active: Mapped[bool] = mapped_column(
        Boolean, default=True, server_default="1", nullable=False, index=True
    )

    def __repr__(self) -> str:  # pragma: no cover - debug helper
        return f"<ProductCategory id={self.id} slug={self.slug!r} active={self.is_active}>"

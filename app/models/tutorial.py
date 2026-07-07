"""Tutorials / knowledge base (Phase 9).

`TutorialCategory` groups `Tutorial` articles. A tutorial carries free-form
`content` (plain text with line breaks — HTML-escaped at render time, since no
Markdown/HTML-sanitiser dependency is present) plus optional `platform` and
`product_type` tags so the bot can offer a "connection guide" filtered to the
buyer's platform. `slug` is a stable, URL-safe id; `is_active` hides drafts from
users while admins still see everything.
"""
from __future__ import annotations

from typing import TYPE_CHECKING

from sqlalchemy import Boolean, ForeignKey, Integer, String, Text
from sqlalchemy.orm import Mapped, mapped_column, relationship

from app.models.base import Base, TimestampMixin

if TYPE_CHECKING:
    from app.models.tutorial import TutorialCategory  # noqa: F401

TUTORIAL_PLATFORMS: tuple[str, ...] = (
    "android", "ios", "windows", "mac", "linux", "general",
)
TUTORIAL_PRODUCT_TYPES: tuple[str, ...] = ("license", "v2ray", "general")


class TutorialCategory(Base, TimestampMixin):
    __tablename__ = "tutorial_categories"

    id: Mapped[int] = mapped_column(primary_key=True)
    title: Mapped[str] = mapped_column(String(200), nullable=False)
    slug: Mapped[str] = mapped_column(String(200), nullable=False, unique=True, index=True)
    sort_order: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    is_active: Mapped[bool] = mapped_column(Boolean, nullable=False, default=True)

    tutorials: Mapped[list["Tutorial"]] = relationship(
        "Tutorial", back_populates="category", lazy="selectin",
        order_by="Tutorial.sort_order, Tutorial.id",
    )

    def __repr__(self) -> str:  # pragma: no cover - debug helper
        return f"<TutorialCategory id={self.id} slug={self.slug!r}>"


class Tutorial(Base, TimestampMixin):
    __tablename__ = "tutorials"

    id: Mapped[int] = mapped_column(primary_key=True)
    category_id: Mapped[int | None] = mapped_column(
        ForeignKey("tutorial_categories.id", ondelete="SET NULL"), nullable=True, index=True
    )
    title: Mapped[str] = mapped_column(String(200), nullable=False)
    slug: Mapped[str] = mapped_column(String(200), nullable=False, unique=True, index=True)
    content: Mapped[str] = mapped_column(Text, nullable=False, default="")

    platform: Mapped[str | None] = mapped_column(String(20), nullable=True, index=True)
    product_type: Mapped[str | None] = mapped_column(String(20), nullable=True, index=True)

    sort_order: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    is_active: Mapped[bool] = mapped_column(Boolean, nullable=False, default=True, index=True)

    category: Mapped["TutorialCategory | None"] = relationship(
        "TutorialCategory", back_populates="tutorials", lazy="selectin"
    )

    def __repr__(self) -> str:  # pragma: no cover - debug helper
        return f"<Tutorial id={self.id} slug={self.slug!r} platform={self.platform}>"

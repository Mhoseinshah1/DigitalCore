"""Tutorials / knowledge base (Phase 9): category + article CRUD.

Content is stored verbatim and rendered as HTML-escaped text with line breaks
(``render_content_html``) — there is no Markdown/HTML-sanitiser dependency, so we
never inject raw user/admin HTML into a page. Slugs are generated from the title,
made URL-safe, and de-duplicated. `is_active=False` hides drafts from users while
admins keep seeing everything.
"""
from __future__ import annotations

import html
import re
from datetime import datetime, timezone

from sqlalchemy import func, or_, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.settings_service import SettingsService
from app.models.tutorial import (
    TUTORIAL_PLATFORMS,
    TUTORIAL_PRODUCT_TYPES,
    Tutorial,
    TutorialCategory,
)
from app.services import audit_service


class TutorialError(ValueError):
    code = "tutorial_error"

    def __init__(self, message: str, code: str | None = None) -> None:
        super().__init__(message)
        if code is not None:
            self.code = code


def _now() -> datetime:
    return datetime.now(timezone.utc)


async def tutorials_enabled(session: AsyncSession) -> bool:
    return await SettingsService(session).get_bool("tutorials_enabled", True)


# --------------------------------------------------------------------------
# Slug + content helpers
# --------------------------------------------------------------------------
def slugify(text: str) -> str:
    """A URL-safe slug. Keeps unicode letters/digits, collapses to hyphens.

    Persian/Arabic titles keep their letters (so the slug is still meaningful);
    only whitespace/punctuation becomes a hyphen.
    """
    s = (text or "").strip().lower()
    s = re.sub(r"[\s_]+", "-", s)
    s = re.sub(r"[^\w\-]", "", s, flags=re.UNICODE)
    s = re.sub(r"-{2,}", "-", s).strip("-")
    return s or "item"


async def _unique_slug(session: AsyncSession, model, base: str, *, exclude_id: int | None = None) -> str:
    """Return `base`, or `base-2`, `base-3`… so the slug is unique for `model`."""
    slug = base
    n = 1
    while True:
        stmt = select(model.id).where(model.slug == slug)
        if exclude_id is not None:
            stmt = stmt.where(model.id != exclude_id)
        clash = await session.scalar(stmt)
        if clash is None:
            return slug
        n += 1
        slug = f"{base}-{n}"


def render_content_html(content: str) -> str:
    """HTML-escape the content and turn newlines into <br> — safe to embed."""
    escaped = html.escape(content or "")
    return escaped.replace("\r\n", "\n").replace("\r", "\n").replace("\n", "<br>")


# --------------------------------------------------------------------------
# Categories
# --------------------------------------------------------------------------
async def get_category(session: AsyncSession, category_id: int) -> TutorialCategory | None:
    return await session.get(TutorialCategory, category_id)


async def list_categories(
    session: AsyncSession, *, active_only: bool = False
) -> list[TutorialCategory]:
    stmt = select(TutorialCategory)
    if active_only:
        stmt = stmt.where(TutorialCategory.is_active.is_(True))
    stmt = stmt.order_by(TutorialCategory.sort_order, TutorialCategory.id)
    return list((await session.execute(stmt)).scalars().all())


async def create_category(
    session: AsyncSession, title: str, *, sort_order: int = 0, is_active: bool = True,
    actor_type: str = "admin", actor_id: int | None = None,
) -> TutorialCategory:
    title = (title or "").strip()
    if not title:
        raise TutorialError("title is required", code="title_required")
    slug = await _unique_slug(session, TutorialCategory, slugify(title))
    cat = TutorialCategory(title=title[:200], slug=slug, sort_order=int(sort_order or 0),
                           is_active=bool(is_active))
    session.add(cat)
    await session.flush()
    await audit_service.log(
        session, actor_type=actor_type, actor_id=actor_id,
        action="tutorial_category_created", target_type="tutorial_category",
        target_id=cat.id, new=f"title={title[:60]!r} slug={slug}",
    )
    await session.refresh(cat)
    return cat


async def update_category(
    session: AsyncSession, category_id: int, *, title: str | None = None,
    sort_order: int | None = None, is_active: bool | None = None,
    actor_type: str = "admin", actor_id: int | None = None,
) -> TutorialCategory | None:
    cat = await get_category(session, category_id)
    if cat is None:
        return None
    if title is not None:
        title = title.strip()
        if not title:
            raise TutorialError("title is required", code="title_required")
        cat.title = title[:200]
    if sort_order is not None:
        cat.sort_order = int(sort_order)
    if is_active is not None:
        cat.is_active = bool(is_active)
    await audit_service.log(
        session, actor_type=actor_type, actor_id=actor_id,
        action="tutorial_category_updated", target_type="tutorial_category",
        target_id=cat.id, new=f"title={cat.title[:60]!r} active={cat.is_active}",
    )
    await session.refresh(cat)
    return cat


# --------------------------------------------------------------------------
# Tutorials
# --------------------------------------------------------------------------
def _normalize_platform(platform: str | None) -> str | None:
    if platform in (None, "", "none"):
        return None
    if platform not in TUTORIAL_PLATFORMS:
        raise TutorialError("invalid platform", code="invalid_platform")
    return platform


def _normalize_product_type(product_type: str | None) -> str | None:
    if product_type in (None, "", "none"):
        return None
    if product_type not in TUTORIAL_PRODUCT_TYPES:
        raise TutorialError("invalid product type", code="invalid_product_type")
    return product_type


async def get_tutorial(session: AsyncSession, tutorial_id: int) -> Tutorial | None:
    return await session.get(Tutorial, tutorial_id)


async def get_tutorial_by_slug(session: AsyncSession, slug: str) -> Tutorial | None:
    return await session.scalar(select(Tutorial).where(Tutorial.slug == slug))


async def list_tutorials(
    session: AsyncSession, *, active_only: bool = False, category_id: int | None = None,
    platform: str | None = None, product_type: str | None = None, limit: int = 200,
) -> list[Tutorial]:
    stmt = select(Tutorial)
    if active_only:
        stmt = stmt.where(Tutorial.is_active.is_(True))
    if category_id is not None:
        stmt = stmt.where(Tutorial.category_id == category_id)
    if platform:
        # A platform-specific request also matches "general" guides.
        stmt = stmt.where(or_(Tutorial.platform == platform,
                              Tutorial.platform == "general",
                              Tutorial.platform.is_(None)))
    if product_type:
        stmt = stmt.where(or_(Tutorial.product_type == product_type,
                              Tutorial.product_type == "general",
                              Tutorial.product_type.is_(None)))
    stmt = stmt.order_by(Tutorial.sort_order, Tutorial.id).limit(limit)
    return list((await session.execute(stmt)).scalars().all())


async def search_tutorials(
    session: AsyncSession, query: str, *, active_only: bool = True, limit: int = 20
) -> list[Tutorial]:
    q = (query or "").strip()
    if not q:
        return []
    like = f"%{q}%"
    stmt = select(Tutorial).where(or_(Tutorial.title.ilike(like),
                                      Tutorial.content.ilike(like)))
    if active_only:
        stmt = stmt.where(Tutorial.is_active.is_(True))
    stmt = stmt.order_by(Tutorial.sort_order, Tutorial.id).limit(limit)
    return list((await session.execute(stmt)).scalars().all())


async def create_tutorial(
    session: AsyncSession, *, title: str, content: str, category_id: int | None = None,
    platform: str | None = None, product_type: str | None = None, sort_order: int = 0,
    is_active: bool = True, actor_type: str = "admin", actor_id: int | None = None,
) -> Tutorial:
    title = (title or "").strip()
    if not title:
        raise TutorialError("title is required", code="title_required")
    platform = _normalize_platform(platform)
    product_type = _normalize_product_type(product_type)
    if category_id is not None and await get_category(session, category_id) is None:
        raise TutorialError("category not found", code="category_not_found")
    slug = await _unique_slug(session, Tutorial, slugify(title))
    tut = Tutorial(
        title=title[:200], slug=slug, content=content or "", category_id=category_id,
        platform=platform, product_type=product_type, sort_order=int(sort_order or 0),
        is_active=bool(is_active),
    )
    session.add(tut)
    await session.flush()
    await audit_service.log(
        session, actor_type=actor_type, actor_id=actor_id, action="tutorial_created",
        target_type="tutorial", target_id=tut.id,
        new=f"title={title[:60]!r} platform={platform} product_type={product_type}",
    )
    await session.refresh(tut)
    return tut


async def update_tutorial(
    session: AsyncSession, tutorial_id: int, *, title: str | None = None,
    content: str | None = None, category_id: int | None = None, platform: str | None = None,
    product_type: str | None = None, sort_order: int | None = None, is_active: bool | None = None,
    actor_type: str = "admin", actor_id: int | None = None,
) -> Tutorial | None:
    tut = await get_tutorial(session, tutorial_id)
    if tut is None:
        return None
    if title is not None:
        title = title.strip()
        if not title:
            raise TutorialError("title is required", code="title_required")
        tut.title = title[:200]
    if content is not None:
        tut.content = content
    if category_id is not None:
        # 0 / negative means "no category".
        cid = int(category_id) or None
        if cid is not None and await get_category(session, cid) is None:
            raise TutorialError("category not found", code="category_not_found")
        tut.category_id = cid
    if platform is not None:
        tut.platform = _normalize_platform(platform)
    if product_type is not None:
        tut.product_type = _normalize_product_type(product_type)
    if sort_order is not None:
        tut.sort_order = int(sort_order)
    if is_active is not None:
        tut.is_active = bool(is_active)
    await audit_service.log(
        session, actor_type=actor_type, actor_id=actor_id, action="tutorial_updated",
        target_type="tutorial", target_id=tut.id, new=f"title={tut.title[:60]!r}",
    )
    await session.refresh(tut)
    return tut


async def toggle_active(
    session: AsyncSession, tutorial_id: int, *, actor_type: str = "admin",
    actor_id: int | None = None,
) -> Tutorial | None:
    tut = await get_tutorial(session, tutorial_id)
    if tut is None:
        return None
    tut.is_active = not tut.is_active
    await audit_service.log(
        session, actor_type=actor_type, actor_id=actor_id, action="tutorial_toggled",
        target_type="tutorial", target_id=tut.id, new=f"active={tut.is_active}",
    )
    await session.refresh(tut)
    return tut

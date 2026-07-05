"""License-key pool: stock keys per product, consume one on license delivery."""
from __future__ import annotations

from datetime import datetime, timezone

from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.models.license_key import LicenseKey
from app.services import audit_service


def _now() -> datetime:
    return datetime.now(timezone.utc)


async def add_keys(
    session: AsyncSession, product_id: int, codes: list[str], *, actor_id: int | None = None
) -> int:
    """Insert unique, non-empty codes for a product. Returns the number added."""
    existing = set(
        (await session.execute(
            select(LicenseKey.code).where(LicenseKey.product_id == product_id)
        )).scalars().all()
    )
    added = 0
    for raw in codes:
        code = (raw or "").strip()
        if not code or code in existing:
            continue
        session.add(LicenseKey(product_id=product_id, code=code))
        existing.add(code)
        added += 1
    if added:
        await session.flush()
        await audit_service.log(
            session, actor_type="admin", actor_id=actor_id, action="license_keys_added",
            target_type="product", target_id=product_id, new=f"count={added}",
        )
    return added


async def available_count(session: AsyncSession, product_id: int) -> int:
    return int(await session.scalar(
        select(func.count(LicenseKey.id)).where(
            LicenseKey.product_id == product_id, LicenseKey.is_used.is_(False)
        )
    ) or 0)


async def list_keys(session: AsyncSession, product_id: int) -> list[LicenseKey]:
    stmt = select(LicenseKey).where(LicenseKey.product_id == product_id).order_by(LicenseKey.id)
    return list((await session.execute(stmt)).scalars().all())


async def assign_next(
    session: AsyncSession, product_id: int, order_id: int
) -> LicenseKey | None:
    """Claim the next unused key for a product. Does not commit."""
    stmt = (
        select(LicenseKey)
        .where(LicenseKey.product_id == product_id, LicenseKey.is_used.is_(False))
        .order_by(LicenseKey.id)
        .limit(1)
    )
    key = await session.scalar(stmt)
    if key is None:
        return None
    key.is_used = True
    key.order_id = order_id
    key.used_at = _now()
    await session.flush()
    return key

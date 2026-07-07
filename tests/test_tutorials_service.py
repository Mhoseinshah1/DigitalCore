"""Phase 9: tutorial_service — category + tutorial CRUD, slug uniqueness, active
filtering (inactive hidden from users), platform matching, content escaping, audit."""
from __future__ import annotations

import pytest
from sqlalchemy import select

from app.models import AuditLog
from app.services import tutorial_service


async def test_category_and_tutorial_crud(db_session) -> None:
    cat = await tutorial_service.create_category(db_session, "Connection guides")
    await db_session.commit()
    assert cat.slug == "connection-guides" and cat.is_active
    tut = await tutorial_service.create_tutorial(
        db_session, title="Android V2Ray", content="step 1\nstep 2",
        category_id=cat.id, platform="android", product_type="v2ray")
    await db_session.commit()
    assert tut.slug == "android-v2ray" and tut.category_id == cat.id
    updated = await tutorial_service.update_tutorial(db_session, tut.id, title="Android v2rayNG")
    await db_session.commit()
    assert updated.title == "Android v2rayNG"


async def test_slug_uniqueness(db_session) -> None:
    a = await tutorial_service.create_tutorial(db_session, title="Setup", content="x")
    b = await tutorial_service.create_tutorial(db_session, title="Setup", content="y")
    await db_session.commit()
    assert a.slug != b.slug and b.slug.startswith("setup")


async def test_inactive_tutorials_hidden_from_users(db_session) -> None:
    t1 = await tutorial_service.create_tutorial(db_session, title="A", content="x")
    await tutorial_service.create_tutorial(db_session, title="B", content="y", is_active=False)
    await db_session.commit()
    active = await tutorial_service.list_tutorials(db_session, active_only=True)
    assert [t.id for t in active] == [t1.id]
    assert len(await tutorial_service.list_tutorials(db_session)) == 2
    # Toggling flips visibility.
    await tutorial_service.toggle_active(db_session, t1.id)
    await db_session.commit()
    assert await tutorial_service.list_tutorials(db_session, active_only=True) == []


async def test_platform_filter_matches_general(db_session) -> None:
    await tutorial_service.create_tutorial(db_session, title="A", content="x", platform="android")
    await tutorial_service.create_tutorial(db_session, title="G", content="y", platform="general")
    await tutorial_service.create_tutorial(db_session, title="I", content="z", platform="ios")
    await db_session.commit()
    android = await tutorial_service.list_tutorials(db_session, active_only=True, platform="android")
    titles = {t.title for t in android}
    assert titles == {"A", "G"}  # android + general, not ios


async def test_content_render_escapes_html(db_session) -> None:
    out = tutorial_service.render_content_html("a <script>x</script>\nb")
    assert "<script>" not in out and "&lt;script&gt;" in out and "<br>" in out


async def test_invalid_platform_and_product_rejected(db_session) -> None:
    with pytest.raises(tutorial_service.TutorialError):
        await tutorial_service.create_tutorial(db_session, title="x", content="y", platform="beos")
    with pytest.raises(tutorial_service.TutorialError):
        await tutorial_service.create_tutorial(db_session, title="x", content="y",
                                               product_type="hardware")


async def test_audit_rows(db_session) -> None:
    cat = await tutorial_service.create_category(db_session, "C", actor_id=1)
    tut = await tutorial_service.create_tutorial(db_session, title="T", content="c", actor_id=1)
    await tutorial_service.update_tutorial(db_session, tut.id, title="T2", actor_id=1)
    await tutorial_service.toggle_active(db_session, tut.id, actor_id=1)
    await tutorial_service.update_category(db_session, cat.id, title="C2", actor_id=1)
    await db_session.commit()
    actions = {r.action for r in (await db_session.execute(select(AuditLog))).scalars().all()}
    assert {"tutorial_category_created", "tutorial_created", "tutorial_updated",
            "tutorial_toggled", "tutorial_category_updated"} <= actions

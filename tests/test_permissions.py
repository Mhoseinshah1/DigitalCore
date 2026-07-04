"""RBAC: role -> permission mapping."""
from __future__ import annotations

from app.core.permissions import ALL_PERMISSIONS, Role, has_permission


def test_owner_has_every_permission() -> None:
    for perm in ALL_PERMISSIONS:
        assert has_permission(Role.OWNER, perm)


def test_viewer_denied_manage_users() -> None:
    assert not has_permission(Role.VIEWER, "manage_users")
    assert has_permission(Role.VIEWER, "view_dashboard")


def test_string_roles_accepted() -> None:
    assert has_permission("owner", "manage_admins")
    assert not has_permission("viewer", "broadcast")


def test_unknown_role_and_none_denied() -> None:
    assert not has_permission("intern", "view_dashboard")
    assert not has_permission(None, "view_dashboard")


def test_accountant_scope() -> None:
    assert has_permission(Role.ACCOUNTANT, "approve_payments")
    assert not has_permission(Role.ACCOUNTANT, "manage_settings")

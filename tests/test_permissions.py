"""RBAC (Phase 2): role -> permission mapping and convenience helpers."""
from __future__ import annotations

from app.core import permissions as P
from app.core.permissions import ALL_PERMISSIONS, Role, has_permission


def test_owner_has_every_permission() -> None:
    for perm in ALL_PERMISSIONS:
        assert has_permission(Role.OWNER, perm)
    assert P.is_owner(Role.OWNER)
    assert P.can_manage_admins(Role.OWNER)


def test_admin_scope() -> None:
    for perm in ("manage_users", "manage_settings", "manage_products",
                 "view_audit_log", "adjust_wallet", "view_payments", "manage_payments"):
        assert has_permission(Role.ADMIN, perm)
    # Admin cannot manage other admins.
    assert not has_permission(Role.ADMIN, "manage_admins")
    assert not P.is_owner(Role.ADMIN)


def test_support_scope() -> None:
    # Can view + block users, nothing financial or settings.
    assert P.can_view_users(Role.SUPPORT)
    assert P.can_manage_users(Role.SUPPORT)
    assert not P.can_adjust_wallet(Role.SUPPORT)
    assert not P.can_manage_settings(Role.SUPPORT)
    assert not P.can_view_payments(Role.SUPPORT)


def test_accountant_scope() -> None:
    assert P.can_view_users(Role.ACCOUNTANT)
    assert P.can_adjust_wallet(Role.ACCOUNTANT)
    assert P.can_view_payments(Role.ACCOUNTANT)
    # Cannot change bot texts / settings, cannot block users.
    assert not P.can_manage_settings(Role.ACCOUNTANT)
    assert not P.can_manage_users(Role.ACCOUNTANT)
    assert not P.can_manage_products(Role.ACCOUNTANT)


def test_viewer_scope() -> None:
    assert P.can_view_dashboard(Role.VIEWER)
    assert P.can_view_users(Role.VIEWER)
    assert not P.can_manage_users(Role.VIEWER)
    assert not P.can_adjust_wallet(Role.VIEWER)
    assert not P.can_manage_settings(Role.VIEWER)


def test_helpers_accept_admin_like_object() -> None:
    class _AdminLike:
        role = "accountant"

    a = _AdminLike()
    assert P.can_adjust_wallet(a)
    assert not P.can_manage_settings(a)
    assert not P.is_owner(a)


def test_string_roles_accepted() -> None:
    assert has_permission("owner", "manage_admins")
    assert not has_permission("viewer", "manage_products")


def test_unknown_role_and_none_denied() -> None:
    assert not has_permission("intern", "view_dashboard")
    assert not has_permission(None, "view_dashboard")
    assert not P.is_owner(None)

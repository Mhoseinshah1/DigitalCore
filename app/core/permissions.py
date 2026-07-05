"""Role-based access control shared by the bot and the web panel.

Roles map to sets of permission strings. The owner always has every permission.
Keep this module dependency-free so anything can import it.

`can_*` helpers accept either a Role, a role string, or any object with a
`.role` attribute (e.g. an Admin), so callers can pass the admin directly.
"""
from __future__ import annotations

from enum import Enum


class Role(str, Enum):
    OWNER = "owner"
    ADMIN = "admin"
    SUPPORT = "support"
    ACCOUNTANT = "accountant"
    VIEWER = "viewer"


# The full permission vocabulary. Extend here as new features land.
ALL_PERMISSIONS: frozenset[str] = frozenset(
    {
        "view_dashboard",
        "view_users",
        "manage_users",
        "adjust_wallet",
        "manage_settings",
        "manage_products",
        "view_payments",
        "manage_payments",
        "view_audit_log",
        "manage_admins",
        "broadcast",
        "manage_xui",
        "approve_payments",
    }
)

PERMISSIONS: dict[Role, frozenset[str]] = {
    # Owner has everything (including manage_admins).
    Role.OWNER: ALL_PERMISSIONS,
    # Admin: manage users/settings/products, adjust wallet, view payments+logs,
    # broadcast + 3X-UI — everything except managing other admins.
    Role.ADMIN: frozenset(
        {
            "view_dashboard",
            "view_users",
            "manage_users",
            "adjust_wallet",
            "manage_settings",
            "manage_products",
            "view_payments",
            "manage_payments",
            "view_audit_log",
            "broadcast",
            "manage_xui",
            "approve_payments",
        }
    ),
    # Support: view users and block/unblock them; no settings or wallet.
    Role.SUPPORT: frozenset({"view_dashboard", "view_users", "manage_users"}),
    # Accountant: view users, adjust wallet, view payment settings/transactions;
    # cannot change bot texts / block users.
    Role.ACCOUNTANT: frozenset(
        {"view_dashboard", "view_users", "adjust_wallet", "view_payments", "approve_payments"}
    ),
    # Viewer: read-only dashboard + users.
    Role.VIEWER: frozenset({"view_dashboard", "view_users"}),
}


def _resolve_role(role: object) -> Role | None:
    """Coerce a Role / role string / object-with-.role into a Role, or None."""
    if role is None:
        return None
    candidate = getattr(role, "role", role)  # unwrap Admin.role if present
    if isinstance(candidate, Role):
        return candidate
    try:
        return Role(str(candidate))
    except ValueError:
        return None


def has_permission(role: object, permission: str) -> bool:
    """True when `role` grants `permission`. Unknown roles/None grant nothing.

    `role` may be a Role, a role string, or an object with a `.role` attribute.
    """
    resolved = _resolve_role(role)
    if resolved is None:
        return False
    return permission in PERMISSIONS.get(resolved, frozenset())


# --------------------------------------------------------------------------
# Convenience helpers (accept a Role, role string, or an Admin object)
# --------------------------------------------------------------------------
def is_owner(role: object) -> bool:
    return _resolve_role(role) is Role.OWNER


def can_view_dashboard(role: object) -> bool:
    return has_permission(role, "view_dashboard")


def can_view_users(role: object) -> bool:
    return has_permission(role, "view_users")


def can_manage_users(role: object) -> bool:
    return has_permission(role, "manage_users")


def can_adjust_wallet(role: object) -> bool:
    return has_permission(role, "adjust_wallet")


def can_manage_settings(role: object) -> bool:
    return has_permission(role, "manage_settings")


def can_manage_products(role: object) -> bool:
    return has_permission(role, "manage_products")


def can_view_payments(role: object) -> bool:
    return has_permission(role, "view_payments")


def can_manage_payments(role: object) -> bool:
    return has_permission(role, "manage_payments")


def can_view_logs(role: object) -> bool:
    return has_permission(role, "view_audit_log")


def can_manage_admins(role: object) -> bool:
    return has_permission(role, "manage_admins")

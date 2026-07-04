"""Role-based access control shared by the bot and the web panel.

Roles map to sets of permission strings. The owner always has every permission.
Keep this module dependency-free so anything can import it.
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
        "manage_users",
        "approve_payments",
        "manage_settings",
        "view_dashboard",
        "broadcast",
        "manage_products",
        "manage_admins",
        "view_audit_log",
    }
)

PERMISSIONS: dict[Role, frozenset[str]] = {
    Role.OWNER: ALL_PERMISSIONS,
    Role.ADMIN: frozenset(
        {
            "manage_users",
            "approve_payments",
            "manage_settings",
            "view_dashboard",
            "broadcast",
            "manage_products",
            "view_audit_log",
        }
    ),
    Role.SUPPORT: frozenset({"view_dashboard", "manage_users"}),
    Role.ACCOUNTANT: frozenset({"view_dashboard", "approve_payments"}),
    Role.VIEWER: frozenset({"view_dashboard"}),
}


def has_permission(role: Role | str | None, permission: str) -> bool:
    """True when `role` grants `permission`. Unknown roles/None grant nothing."""
    if role is None:
        return False
    try:
        resolved = role if isinstance(role, Role) else Role(str(role))
    except ValueError:
        return False
    return permission in PERMISSIONS.get(resolved, frozenset())

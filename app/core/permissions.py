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
        # Phase 4 — receipt-review admin actions.
        "process_payments",  # approve / reject a submitted receipt
        "block_users",
        "restrict_users",
        # Phase 5 — license stock.
        "view_licenses",         # see license pages (no passwords)
        "view_license_secrets",  # reveal a license password on the detail page
        "import_licenses",       # import / add licenses
        "manage_licenses",       # block / mark-broken / redeliver / replace
        # Phase 6 — V2Ray services.
        "view_services",         # see v2ray service pages + refresh usage
        "manage_services",       # provision-retry / disable / enable / delete / reset
        # Phase 7 — wallet.
        "view_wallet_topups",    # see wallet top-up requests + transactions
        "manage_wallet_topups",  # approve / reject wallet top-ups
        "refund_payments",       # refund an order's charge to the wallet
        # Phase 9 — support tickets + tutorials.
        "view_tickets",          # see ticket pages (read-only)
        "manage_tickets",        # reply / close / assign / set priority
        "manage_tutorials",      # create / edit / toggle tutorials + categories
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
            "process_payments",
            "block_users",
            "restrict_users",
            "view_licenses",
            "view_license_secrets",
            "import_licenses",
            "manage_licenses",
            "view_services",
            "manage_services",
            "view_wallet_topups",
            "manage_wallet_topups",
            "refund_payments",
            "view_tickets",
            "manage_tickets",
            "manage_tutorials",
        }
    ),
    # Support: view users, block/restrict them, view license status (no secrets,
    # no import); view v2ray services + refresh usage; view wallet top-ups (no
    # approve); front-line support MANAGES tickets (reply/close/assign/priority).
    Role.SUPPORT: frozenset(
        {"view_dashboard", "view_users", "manage_users", "block_users",
         "restrict_users", "view_licenses", "view_services", "view_wallet_topups",
         "view_tickets", "manage_tickets"}
    ),
    # Accountant: view users, adjust wallet, view + process payments; approve /
    # reject wallet top-ups and refund payments; can VIEW tickets but not manage
    # them; cannot change bot texts or block/restrict users.
    Role.ACCOUNTANT: frozenset(
        {"view_dashboard", "view_users", "adjust_wallet", "view_payments",
         "approve_payments", "process_payments", "view_licenses", "view_services",
         "view_wallet_topups", "manage_wallet_topups", "refund_payments",
         "view_tickets"}
    ),
    # Viewer: read-only dashboard + users + license/service/wallet counts + tickets.
    Role.VIEWER: frozenset(
        {"view_dashboard", "view_users", "view_licenses", "view_services",
         "view_wallet_topups", "view_tickets"}
    ),
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


def can_manage_xui_servers(role: object) -> bool:
    return has_permission(role, "manage_xui")


def can_manage_xui_inbounds(role: object) -> bool:
    return has_permission(role, "manage_xui")


def can_process_payments(role: object) -> bool:
    """Approve or reject a submitted receipt."""
    return has_permission(role, "process_payments")


def can_block_users(role: object) -> bool:
    return has_permission(role, "block_users")


def can_restrict_users(role: object) -> bool:
    return has_permission(role, "restrict_users")


def can_view_licenses(role: object) -> bool:
    return has_permission(role, "view_licenses")


def can_manage_licenses(role: object) -> bool:
    return has_permission(role, "manage_licenses")


def can_view_license_secrets(role: object) -> bool:
    return has_permission(role, "view_license_secrets")


def can_import_licenses(role: object) -> bool:
    return has_permission(role, "import_licenses")


def can_view_services(role: object) -> bool:
    return has_permission(role, "view_services")


def can_manage_services(role: object) -> bool:
    return has_permission(role, "manage_services")


def can_view_wallet_topups(role: object) -> bool:
    return has_permission(role, "view_wallet_topups")


def can_manage_wallet_topups(role: object) -> bool:
    return has_permission(role, "manage_wallet_topups")


def can_refund_payments(role: object) -> bool:
    return has_permission(role, "refund_payments")


def can_view_tickets(role: object) -> bool:
    return has_permission(role, "view_tickets")


def can_manage_tickets(role: object) -> bool:
    """Reply / close / assign / set priority on tickets."""
    return has_permission(role, "manage_tickets")


def can_manage_tutorials(role: object) -> bool:
    return has_permission(role, "manage_tutorials")

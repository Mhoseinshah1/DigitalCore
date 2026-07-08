"""Server-rendered admin panel (Persian RTL) served under /admin.

Everything the admin sees lives under the /admin prefix; `/` and `/login`
redirect there for convenience. The viewer's language comes from the dc_lang
cookie (default fa) and is exposed to templates as `lang`, `rtl`, and the `_`
translator. Auth is a JWT session cookie (see app/web/deps.py).
"""
from __future__ import annotations

import shutil
from pathlib import Path
from urllib.parse import quote

from fastapi import APIRouter, Depends, Form, Request
from fastapi.responses import (
    FileResponse,
    HTMLResponse,
    JSONResponse,
    RedirectResponse,
    Response,
)
from fastapi.templating import Jinja2Templates
from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

from app import __version__
from app.core.defaults import (
    CATEGORIES,
    DEFAULTS,
    category_title_for,
    description_for,
    keys_for_category,
    label_for,
)
from app.core.permissions import has_permission
from app.core.security import create_access_token
from app.core.settings_service import SettingsService, coerce_out
from app.database import get_session
from app.i18n import SUPPORTED, is_rtl, normalize_lang, t
from app.models.admin import Admin
from app.models.payment import Payment
from app.models.product import PRODUCT_TYPES, Product
from app.core.statuses import (
    order_status_label,
    payment_status_label,
    topup_status_label,
    wallet_tx_type_label,
)
from app.schemas.product import ProductCreate, ProductUpdate
from app.services import (
    audit_service,
    backup_service,
    coupon_service,
    export_service,
    license_service,
    order_service,
    payment_service,
    product_service,
    referral_service,
    report_service,
    restore_service,
    ticket_service,
    tutorial_service,
    user_service,
    v2ray_lifecycle_service,
    v2ray_service,
    wallet_service,
    xui_server_service,
)
from app.web.api.auth import authenticate_admin
from app.web.deps import COOKIE_NAME, get_current_admin_optional, set_session_cookie

BASE_DIR = Path(__file__).resolve().parent
templates = Jinja2Templates(directory=BASE_DIR / "templates")


def render_template(name: str, context: dict, status_code: int = 200):
    """Render a Jinja template in a way that is stable across Starlette versions.

    Starlette >= 0.29 made ``request`` the first positional argument of
    ``TemplateResponse`` (``request, name, context``). Older code that called
    ``TemplateResponse(name, context)`` therefore had the context dict treated
    as the template *name*, which crashes with ``TypeError: unhashable type:
    'dict'`` when Jinja tries to use it as a cache key (seen after the FastAPI
    0.115 -> 0.139 / Starlette 0.41 -> 1.x bump).

    Every context here is built by ``_ctx`` and always carries ``request``, so
    we pull it out and call ``TemplateResponse`` with explicit keyword
    arguments that are accepted by both the old and new Starlette signatures.
    """
    context = dict(context or {})
    request = context.get("request")
    context.setdefault("request", request)
    return templates.TemplateResponse(
        request=request, name=name, context=context, status_code=status_code
    )


# Panel routes live under /admin; a couple of root redirects point here.
router = APIRouter(prefix="/admin", include_in_schema=False)
root_router = APIRouter(include_in_schema=False)

LANG_COOKIE = "dc_lang"
LOGIN_PATH = "/admin/login"

# Which settings page (URL slug) each catalog category renders on, and the
# reverse map used by the settings routes.
SETTINGS_PAGES: dict[str, str] = {
    "general": "general",
    "telegram": "telegram",
    "payment": "payment",
    "bot-texts": "texts",
}


# --------------------------------------------------------------------------
# Navigation (grouped, RTL sidebar). Every entry carries the permission needed
# to see it; `placeholder=True` routes to a "coming soon" page.
# --------------------------------------------------------------------------
NAV_TREE: list[dict] = [
    {"label_key": "nav.dashboard", "icon": "🏠", "href": "/admin",
     "permission": "view_dashboard"},

    {"label_key": "nav.users", "icon": "👥", "children": [
        {"label_key": "nav.users.all", "icon": "📋", "href": "/admin/users",
         "permission": "view_users"},
        {"label_key": "nav.users.blocked", "icon": "🚫", "href": "/admin/users/blocked",
         "permission": "view_users"},
        {"label_key": "nav.users.wallet", "icon": "💰", "href": "/admin/users/wallet",
         "permission": "view_users"},
        {"label_key": "nav.users.activity", "icon": "📈", "href": "/admin/users/activity",
         "permission": "view_users", "placeholder": True},
    ]},

    {"label_key": "nav.products", "icon": "📦", "children": [
        {"label_key": "nav.products.all", "icon": "📦", "href": "/admin/products",
         "permission": "manage_products"},
        {"label_key": "nav.products.create", "icon": "➕", "href": "/admin/products/create",
         "permission": "manage_products"},
        {"label_key": "nav.products.license", "icon": "🔑", "href": "/admin/products/license",
         "permission": "manage_products", "placeholder": True},
        {"label_key": "nav.products.v2ray", "icon": "🌐", "href": "/admin/products/v2ray",
         "permission": "manage_products", "placeholder": True},
    ]},

    {"label_key": "nav.xui", "icon": "🛰", "children": [
        {"label_key": "nav.xui.servers", "icon": "🖥", "href": "/admin/xui-servers",
         "permission": "manage_xui"},
        {"label_key": "nav.xui.inbounds", "icon": "🔌", "href": "/admin/xui-inbounds",
         "permission": "manage_xui"},
        {"label_key": "nav.xui.v2ray_products", "icon": "🌐", "href": "/admin/products",
         "permission": "manage_products"},
    ]},

    {"label_key": "nav.orders", "icon": "🧾", "children": [
        {"label_key": "nav.orders.all", "icon": "🧾", "href": "/admin/orders",
         "permission": "view_payments"},
        {"label_key": "nav.orders.pending", "icon": "⏳", "href": "/admin/orders/pending-receipts",
         "permission": "view_payments"},
    ]},

    {"label_key": "nav.licenses", "icon": "🔑", "children": [
        {"label_key": "nav.licenses.stock", "icon": "🔑", "href": "/admin/licenses",
         "permission": "view_licenses"},
        {"label_key": "nav.licenses.import", "icon": "📥", "href": "/admin/licenses/import",
         "permission": "import_licenses"},
        {"label_key": "nav.licenses.sold", "icon": "✅", "href": "/admin/licenses/sold",
         "permission": "view_licenses"},
        {"label_key": "nav.licenses.low_stock", "icon": "📉", "href": "/admin/licenses/low-stock",
         "permission": "view_licenses"},
    ]},

    {"label_key": "nav.services", "icon": "🌐", "children": [
        {"label_key": "nav.services.all", "icon": "🌐", "href": "/admin/v2ray-services",
         "permission": "view_services"},
        {"label_key": "nav.services.active", "icon": "✅",
         "href": "/admin/v2ray-services?status=active", "permission": "view_services"},
        {"label_key": "nav.services.failed", "icon": "⚠️",
         "href": "/admin/v2ray-services?status=failed", "permission": "view_services"},
    ]},

    {"label_key": "nav.wallet", "icon": "💰", "children": [
        {"label_key": "nav.wallet.topups", "icon": "🧾", "href": "/admin/wallet/topups",
         "permission": "view_wallet_topups"},
        {"label_key": "nav.wallet.pending", "icon": "⏳",
         "href": "/admin/wallet/topups/pending", "permission": "view_wallet_topups"},
        {"label_key": "nav.wallet.transactions", "icon": "📊",
         "href": "/admin/wallet/transactions", "permission": "view_wallet_topups"},
    ]},

    {"label_key": "nav.support", "icon": "🎫", "children": [
        {"label_key": "nav.support.tickets", "icon": "🎫", "href": "/admin/tickets",
         "permission": "view_tickets"},
        {"label_key": "nav.support.open", "icon": "📨",
         "href": "/admin/tickets?status=open", "permission": "view_tickets"},
        {"label_key": "nav.support.closed", "icon": "✅",
         "href": "/admin/tickets?status=closed", "permission": "view_tickets"},
        {"label_key": "nav.support.mine", "icon": "🙋",
         "href": "/admin/tickets?assigned=me", "permission": "view_tickets"},
    ]},

    {"label_key": "nav.tutorials", "icon": "📚", "children": [
        {"label_key": "nav.tutorials.all", "icon": "📚", "href": "/admin/tutorials",
         "permission": "manage_tutorials"},
        {"label_key": "nav.tutorials.create", "icon": "➕", "href": "/admin/tutorials/create",
         "permission": "manage_tutorials"},
        {"label_key": "nav.tutorials.categories", "icon": "🗂",
         "href": "/admin/tutorial-categories", "permission": "manage_tutorials"},
    ]},

    {"label_key": "nav.marketing", "icon": "🏷", "children": [
        {"label_key": "nav.marketing.coupons", "icon": "🏷", "href": "/admin/coupons",
         "permission": "view_coupons"},
        {"label_key": "nav.marketing.create_coupon", "icon": "➕",
         "href": "/admin/coupons/create", "permission": "manage_coupons"},
        {"label_key": "nav.marketing.referrals", "icon": "🔗", "href": "/admin/referrals",
         "permission": "manage_referrals"},
        {"label_key": "nav.marketing.rewards", "icon": "🎁",
         "href": "/admin/referral-rewards", "permission": "manage_referrals"},
    ]},

    {"label_key": "nav.reports", "icon": "📊", "children": [
        {"label_key": "nav.reports.overview", "icon": "📊", "href": "/admin/reports",
         "permission": "view_reports"},
        {"label_key": "nav.reports.sales", "icon": "💵", "href": "/admin/reports/sales",
         "permission": "view_financial_reports"},
        {"label_key": "nav.reports.orders", "icon": "🧾", "href": "/admin/reports/orders",
         "permission": "view_reports"},
        {"label_key": "nav.reports.payments", "icon": "💳", "href": "/admin/reports/payments",
         "permission": "view_financial_reports"},
        {"label_key": "nav.reports.wallet", "icon": "👛", "href": "/admin/reports/wallet",
         "permission": "view_financial_reports"},
        {"label_key": "nav.reports.products", "icon": "📦", "href": "/admin/reports/products",
         "permission": "view_reports"},
        {"label_key": "nav.reports.users", "icon": "📈", "href": "/admin/reports/users",
         "permission": "view_user_reports"},
        {"label_key": "nav.reports.licenses", "icon": "🔑", "href": "/admin/reports/licenses",
         "permission": "view_service_reports"},
        {"label_key": "nav.reports.v2ray", "icon": "🌐", "href": "/admin/reports/v2ray",
         "permission": "view_service_reports"},
        {"label_key": "nav.reports.marketing", "icon": "🏷", "href": "/admin/reports/marketing",
         "permission": "view_financial_reports"},
        {"label_key": "nav.reports.support", "icon": "🎫", "href": "/admin/reports/support",
         "permission": "view_reports"},
        {"label_key": "nav.reports.exports", "icon": "📤", "href": "/admin/reports/exports",
         "permission": "export_reports"},
    ]},

    {"label_key": "nav.maintenance", "icon": "🧰", "children": [
        {"label_key": "nav.maintenance.overview", "icon": "🧰", "href": "/admin/maintenance",
         "permission": "view_maintenance"},
        {"label_key": "nav.maintenance.backups", "icon": "💾", "href": "/admin/maintenance/backups",
         "permission": "manage_backups"},
        {"label_key": "nav.maintenance.restore", "icon": "♻️", "href": "/admin/maintenance/restore",
         "permission": "restore_backups"},
        {"label_key": "nav.maintenance.health", "icon": "🩺", "href": "/admin/maintenance/health",
         "permission": "view_health"},
        {"label_key": "nav.maintenance.system", "icon": "ℹ️", "href": "/admin/maintenance/system-info",
         "permission": "view_maintenance"},
    ]},

    {"label_key": "nav.payments", "icon": "💳", "children": [
        {"label_key": "nav.payments.settings", "icon": "💳", "href": "/admin/settings/payment",
         "permission": "view_payments"},
        {"label_key": "nav.payments.wallet", "icon": "📊", "href": "/admin/payments/wallet",
         "permission": "view_payments", "placeholder": True},
        {"label_key": "nav.payments.receipts", "icon": "🧾", "href": "/admin/payments/receipts",
         "permission": "view_payments", "placeholder": True},
    ]},

    {"label_key": "nav.bot", "icon": "🤖", "children": [
        {"label_key": "nav.bot.messages", "icon": "📝", "href": "/admin/settings/bot-texts",
         "permission": "manage_settings"},
    ]},

    {"label_key": "nav.system", "icon": "⚙️", "children": [
        {"label_key": "nav.system.general", "icon": "⚙️", "href": "/admin/settings/general",
         "permission": "manage_settings"},
        {"label_key": "nav.system.telegram", "icon": "✈️", "href": "/admin/settings/telegram",
         "permission": "manage_settings"},
        {"label_key": "nav.system.maintenance", "icon": "🛠", "href": "/admin/settings/general#maintenance_mode",
         "permission": "manage_settings"},
        {"label_key": "nav.system.sales", "icon": "🛒", "href": "/admin/settings/general#sales_enabled",
         "permission": "manage_settings"},
    ]},

    {"label_key": "nav.logs", "icon": "📜", "children": [
        {"label_key": "nav.logs.audit", "icon": "📜", "href": "/admin/audit-logs",
         "permission": "view_audit_log"},
        {"label_key": "nav.logs.admin", "icon": "🛡", "href": "/admin/audit-logs?scope=admin",
         "permission": "view_audit_log"},
    ]},

    {"label_key": "nav.future", "icon": "🧭", "children": [
        {"label_key": "nav.future.services", "icon": "🌐", "href": "/admin/services",
         "permission": "manage_products", "placeholder": True},
        {"label_key": "nav.future.backups", "icon": "💾", "href": "/admin/backups",
         "permission": "manage_settings", "placeholder": True},
    ]},
]

# Placeholder pages: (path, i18n title key, permission). Real routes handle the
# rest; these are the not-yet-built sections referenced by NAV_TREE.
PLACEHOLDER_PAGES: list[tuple[str, str, str]] = [
    ("/users/activity", "nav.users.activity", "view_users"),
    ("/products/license", "nav.products.license", "manage_products"),
    ("/products/v2ray", "nav.products.v2ray", "manage_products"),
    ("/payments/wallet", "nav.payments.wallet", "view_payments"),
    ("/payments/receipts", "nav.payments.receipts", "view_payments"),
    ("/services", "nav.future.services", "manage_products"),
    ("/backups", "nav.future.backups", "manage_settings"),
]


def _resolve_lang(request: Request) -> str:
    return normalize_lang(request.cookies.get(LANG_COOKIE))


def _can(role: object, permission: str | None) -> bool:
    return permission is None or has_permission(role, permission)


def _item_path(href: str) -> str:
    """The routeable path of an href, dropping any #fragment or ?query."""
    return href.split("#", 1)[0].split("?", 1)[0]


def build_nav(role: object, lang: str, current_path: str) -> list[dict]:
    """RBAC-filtered, render-ready navigation for the current viewer.

    Groups with no visible child are omitted; direct links the role can't access
    are hidden. Active state is computed from the request path.
    """
    sections: list[dict] = []
    for node in NAV_TREE:
        if "children" in node:
            children = []
            for it in node["children"]:
                if not _can(role, it.get("permission")):
                    continue
                children.append({
                    "label": t(it["label_key"], lang),
                    "href": it["href"],
                    "icon": it.get("icon", "•"),
                    "active": current_path == _item_path(it["href"]),
                    "placeholder": bool(it.get("placeholder")),
                })
            if not children:
                continue
            sections.append({
                "label": t(node["label_key"], lang),
                "icon": node.get("icon", "•"),
                "href": None,
                "active": any(c["active"] for c in children),
                "children": children,
            })
            continue

        if not _can(role, node.get("permission")):
            continue
        sections.append({
            "label": t(node["label_key"], lang),
            "icon": node.get("icon", "•"),
            "href": node["href"],
            "active": current_path == _item_path(node["href"]),
            "children": [],
            "placeholder": bool(node.get("placeholder")),
        })
    return sections


def _ctx(request: Request, admin: Admin | None, **extra: object) -> dict:
    lang = _resolve_lang(request)
    role = admin.role if admin is not None else None
    return {
        "request": request,
        "admin": admin,
        "nav": build_nav(role, lang, request.url.path),
        "version": __version__,
        "domain": request.url.hostname,
        "lang": lang,
        "rtl": is_rtl(lang),
        "_": lambda key, **params: t(key, lang, **params),
        **extra,
    }


def _forbidden(lang: str) -> HTMLResponse:
    body = (
        f"<h1>{t('web.forbidden_title', lang)}</h1>"
        f"<p>{t('web.forbidden_body', lang)}</p>"
    )
    return HTMLResponse(body, status_code=403)


def _guard(request: Request, admin: Admin | None, permission: str | None = None):
    """Return (lang, deny_response|None). `deny` is a redirect/403 to return early."""
    lang = _resolve_lang(request)
    if admin is None:
        return lang, RedirectResponse(LOGIN_PATH, status_code=302)
    if permission and not has_permission(admin.role, permission):
        return lang, _forbidden(lang)
    return lang, None


def _client_ip(request: Request) -> str | None:
    fwd = request.headers.get("x-forwarded-for")
    if fwd:
        return fwd.split(",")[0].strip()
    return request.client.host if request.client else None


# --------------------------------------------------------------------------
# Root redirects + placeholders
# --------------------------------------------------------------------------
@root_router.get("/")
async def root_redirect():
    return RedirectResponse("/admin", status_code=302)


@root_router.get("/login")
async def root_login_redirect():
    return RedirectResponse(LOGIN_PATH, status_code=302)


def _make_placeholder(title_key: str, permission: str):
    async def handler(
        request: Request,
        admin: Admin | None = Depends(get_current_admin_optional),
    ):
        lang, deny = _guard(request, admin, permission)
        if deny:
            return deny
        return render_template(
            "placeholder.html", _ctx(request, admin, page_title_key=title_key)
        )

    return handler


for _path, _title_key, _perm in PLACEHOLDER_PAGES:
    router.add_api_route(
        _path, _make_placeholder(_title_key, _perm),
        methods=["GET"], response_class=HTMLResponse, include_in_schema=False,
    )


# --------------------------------------------------------------------------
# Auth
# --------------------------------------------------------------------------
@router.get("/lang/{code}")
async def switch_language(code: str, request: Request):
    """Set the panel language cookie and bounce back to the referring page."""
    lang = normalize_lang(code) if code in SUPPORTED else None
    target = request.headers.get("referer") or "/admin"
    resp = RedirectResponse(target, status_code=302)
    if lang:
        resp.set_cookie(LANG_COOKIE, lang, max_age=365 * 24 * 3600, samesite="lax")
    return resp


@router.get("/login", response_class=HTMLResponse)
async def login_page(request: Request, admin: Admin | None = Depends(get_current_admin_optional)):
    if admin is not None:
        return RedirectResponse("/admin", status_code=302)
    return render_template("login.html", _ctx(request, None, error=None))


@router.post("/login", response_class=HTMLResponse)
async def login_submit(
    request: Request,
    username: str = Form(...),
    password: str = Form(...),
    session: AsyncSession = Depends(get_session),
):
    admin = await authenticate_admin(session, username, password)
    if admin is None:
        lang = _resolve_lang(request)
        return render_template(
            "login.html",
            _ctx(request, None, error=t("web.invalid_credentials", lang)),
            status_code=401,
        )
    token = create_access_token(admin.id, username=admin.username, email=admin.email)
    await audit_service.log(
        session, actor_type="admin", actor_id=admin.id, action="admin.login",
        target_type="admin", target_id=admin.id, ip_address=_client_ip(request),
    )
    resp = RedirectResponse("/admin", status_code=302)
    set_session_cookie(resp, token, request=request)
    return resp


@router.get("/logout")
async def logout():
    resp = RedirectResponse(LOGIN_PATH, status_code=302)
    resp.delete_cookie(COOKIE_NAME)
    return resp


# --------------------------------------------------------------------------
# Dashboard
# --------------------------------------------------------------------------
@router.get("", response_class=HTMLResponse)
async def dashboard(
    request: Request,
    admin: Admin | None = Depends(get_current_admin_optional),
    session: AsyncSession = Depends(get_session),
):
    lang, deny = _guard(request, admin, "view_dashboard")
    if deny:
        return deny
    stats = await user_service.get_stats(session)
    active_products = await session.scalar(
        select(func.count(Product.id)).where(
            Product.is_active.is_(True), Product.is_hidden.is_(False)
        )
    ) or 0
    svc = SettingsService(session)
    site_name = await svc.get_str("site_name", "DigitalCore")
    maintenance = await svc.get_bool("maintenance_mode", False)
    sales = await svc.get_bool("sales_enabled", True)
    # Phase 11: a 30-day analytics snapshot powered by report_service. Revenue
    # is only surfaced to admins who may view financial reports; the operational
    # "needs attention" counters are safe for everyone with view_dashboard.
    r_start, r_end = report_service.parse_date_range(preset="last_30_days")
    summary = await report_service.get_dashboard_summary(session, r_start, r_end)
    return render_template(
        "dashboard.html",
        _ctx(
            request, admin,
            stats=stats,
            active_products=int(active_products),
            site_name=site_name,
            maintenance=maintenance,
            sales=sales,
            summary=summary,
            can_reports=has_permission(admin.role, "view_reports"),
            can_financial=has_permission(admin.role, "view_financial_reports"),
        ),
    )


# --------------------------------------------------------------------------
# Users
# --------------------------------------------------------------------------
@router.get("/users", response_class=HTMLResponse)
async def users_page(
    request: Request,
    admin: Admin | None = Depends(get_current_admin_optional),
    session: AsyncSession = Depends(get_session),
    q: str = "",
    saved: int = 0,
    error: str = "",
):
    lang, deny = _guard(request, admin, "view_users")
    if deny:
        return deny
    users = await user_service.list_users(session, search=(q or None))
    return render_template(
        "users_list.html",
        _ctx(request, admin, users=users, q=q, blocked_only=False,
             saved=bool(saved), error=error),
    )


@router.get("/users/blocked", response_class=HTMLResponse)
async def users_blocked_page(
    request: Request,
    admin: Admin | None = Depends(get_current_admin_optional),
    session: AsyncSession = Depends(get_session),
):
    lang, deny = _guard(request, admin, "view_users")
    if deny:
        return deny
    users = await user_service.list_blocked_users(session)
    return render_template(
        "users_list.html",
        _ctx(request, admin, users=users, q="", blocked_only=True, saved=False, error=""),
    )


@router.get("/users/wallet", response_class=HTMLResponse)
async def wallet_adjustments_page(
    request: Request,
    admin: Admin | None = Depends(get_current_admin_optional),
    session: AsyncSession = Depends(get_session),
):
    lang, deny = _guard(request, admin, "view_users")
    if deny:
        return deny
    txns = await user_service.list_wallet_transactions(session)
    # Attach a display label per user for the template.
    user_ids = {tx.user_id for tx in txns}
    labels: dict[int, str] = {}
    for uid in user_ids:
        u = await user_service.get_by_id(session, uid)
        if u is not None:
            labels[uid] = u.username or (u.telegram_id and str(u.telegram_id)) or f"#{u.id}"
    return render_template(
        "wallet_transactions.html",
        _ctx(request, admin, txns=txns, labels=labels),
    )


@router.get("/users/{user_id}", response_class=HTMLResponse)
async def user_detail_page(
    user_id: int,
    request: Request,
    admin: Admin | None = Depends(get_current_admin_optional),
    session: AsyncSession = Depends(get_session),
    saved: int = 0,
    error: str = "",
):
    lang, deny = _guard(request, admin, "view_users")
    if deny:
        return deny
    user = await user_service.get_by_id(session, user_id)
    if user is None:
        return RedirectResponse("/admin/users", status_code=302)
    summary = user_service.get_user_summary(user)
    txns = await user_service.list_wallet_transactions(session, user_id=user_id, limit=20)
    orders = await order_service.list_user_orders(session, user_id, limit=20)
    order_rows = [_order_row(o, lang) for o in orders]
    return render_template(
        "user_detail.html",
        _ctx(request, admin, user=user, summary=summary, txns=txns,
             orders=order_rows,
             can_manage_users=has_permission(admin.role, "manage_users"),
             can_adjust_wallet=has_permission(admin.role, "adjust_wallet"),
             can_block_users=has_permission(admin.role, "block_users"),
             can_restrict_users=has_permission(admin.role, "restrict_users"),
             saved=bool(saved), error=error),
    )


@router.post("/users/{user_id}/restrict")
async def user_restrict(
    user_id: int,
    request: Request,
    admin: Admin | None = Depends(get_current_admin_optional),
    session: AsyncSession = Depends(get_session),
):
    lang, deny = _guard(request, admin, "restrict_users")
    if deny:
        return deny
    form = dict(await request.form())
    reason = str(form.get("reason", "")).strip()
    if not reason:
        return RedirectResponse(
            f"/admin/users/{user_id}?error={quote('a reason is required')}", status_code=303
        )
    await user_service.set_restricted(
        session, user_id, True, reason=reason, actor_id=admin.id,
        ip_address=_client_ip(request),
    )
    await audit_service.log(
        session, actor_type="admin", actor_id=admin.id, action="user_restricted",
        target_type="user", target_id=user_id, meta=f"reason={reason}",
    )
    await session.commit()
    return RedirectResponse(f"/admin/users/{user_id}?saved=1", status_code=303)


@router.post("/users/{user_id}/unrestrict")
async def user_unrestrict(
    user_id: int,
    request: Request,
    admin: Admin | None = Depends(get_current_admin_optional),
    session: AsyncSession = Depends(get_session),
):
    lang, deny = _guard(request, admin, "restrict_users")
    if deny:
        return deny
    await user_service.set_restricted(
        session, user_id, False, actor_id=admin.id, ip_address=_client_ip(request)
    )
    await audit_service.log(
        session, actor_type="admin", actor_id=admin.id, action="user_unrestricted",
        target_type="user", target_id=user_id,
    )
    await session.commit()
    return RedirectResponse(f"/admin/users/{user_id}?saved=1", status_code=303)


@router.post("/users/{user_id}/block")
async def user_block(
    user_id: int,
    request: Request,
    admin: Admin | None = Depends(get_current_admin_optional),
    session: AsyncSession = Depends(get_session),
):
    lang, deny = _guard(request, admin, "manage_users")
    if deny:
        return deny
    await user_service.admin_set_blocked(
        session, user_id, True, actor_id=admin.id, ip_address=_client_ip(request)
    )
    await session.commit()
    return RedirectResponse(f"/admin/users/{user_id}?saved=1", status_code=303)


@router.post("/users/{user_id}/unblock")
async def user_unblock(
    user_id: int,
    request: Request,
    admin: Admin | None = Depends(get_current_admin_optional),
    session: AsyncSession = Depends(get_session),
):
    lang, deny = _guard(request, admin, "manage_users")
    if deny:
        return deny
    await user_service.admin_set_blocked(
        session, user_id, False, actor_id=admin.id, ip_address=_client_ip(request)
    )
    await session.commit()
    return RedirectResponse(f"/admin/users/{user_id}?saved=1", status_code=303)


@router.post("/users/{user_id}/verify")
async def user_verify(
    user_id: int,
    request: Request,
    admin: Admin | None = Depends(get_current_admin_optional),
    session: AsyncSession = Depends(get_session),
):
    lang, deny = _guard(request, admin, "manage_users")
    if deny:
        return deny
    form = dict(await request.form())
    verified = str(form.get("verified", "")).strip() in ("1", "true", "on", "yes")
    await user_service.set_verified(
        session, user_id, verified, actor_id=admin.id, ip_address=_client_ip(request)
    )
    await session.commit()
    return RedirectResponse(f"/admin/users/{user_id}?saved=1", status_code=303)


@router.post("/users/{user_id}/note")
async def user_note(
    user_id: int,
    request: Request,
    admin: Admin | None = Depends(get_current_admin_optional),
    session: AsyncSession = Depends(get_session),
):
    lang, deny = _guard(request, admin, "manage_users")
    if deny:
        return deny
    form = dict(await request.form())
    note = str(form.get("admin_note", ""))
    await user_service.update_admin_note(
        session, user_id, note, actor_id=admin.id, ip_address=_client_ip(request)
    )
    await session.commit()
    return RedirectResponse(f"/admin/users/{user_id}?saved=1", status_code=303)


@router.post("/users/{user_id}/wallet-adjust")
async def user_wallet_adjust(
    user_id: int,
    request: Request,
    admin: Admin | None = Depends(get_current_admin_optional),
    session: AsyncSession = Depends(get_session),
):
    lang, deny = _guard(request, admin, "adjust_wallet")
    if deny:
        return deny
    form = dict(await request.form())
    direction = str(form.get("direction", "add")).strip()
    reason = str(form.get("reason", "")).strip() or None
    try:
        raw = int(str(form.get("amount", "0")).strip() or "0")
        if raw <= 0:
            raise ValueError("amount must be a positive number")
        amount = raw if direction == "add" else -raw
        await user_service.adjust_wallet_balance(
            session, user_id, amount, reason=reason,
            actor_type="admin", actor_id=admin.id, ip_address=_client_ip(request),
        )
        await session.commit()
    except ValueError as exc:
        return RedirectResponse(
            f"/admin/users/{user_id}?error={quote(str(exc))}", status_code=303
        )
    return RedirectResponse(f"/admin/users/{user_id}?saved=1", status_code=303)


# --------------------------------------------------------------------------
# Settings (general / telegram / payment / bot-texts)
# --------------------------------------------------------------------------
def _settings_items(rows: dict, category: str, lang: str) -> list[dict]:
    items = []
    for d in keys_for_category(category):
        row = rows.get(d.key)
        if d.is_secret:
            value: object = ""
        elif row is None:
            value = coerce_out(d.value_type, d.default)
        else:
            value = coerce_out(d.value_type, row.value)
        items.append({
            "key": d.key,
            "label": label_for(d, lang),
            "description": description_for(d, lang),
            "value_type": d.value_type,
            "is_secret": d.is_secret,
            "value": value,
            "has_secret": bool(row and row.is_secret and row.value),
        })
    return items


@router.get("/settings")
async def settings_root():
    return RedirectResponse("/admin/settings/general", status_code=302)


@router.get("/settings/{page}", response_class=HTMLResponse)
async def settings_page(
    page: str,
    request: Request,
    admin: Admin | None = Depends(get_current_admin_optional),
    session: AsyncSession = Depends(get_session),
    saved: int = 0,
    error: str = "",
):
    category = SETTINGS_PAGES.get(page)
    if category is None:
        return RedirectResponse("/admin/settings/general", status_code=302)
    view_perm = "view_payments" if page == "payment" else "manage_settings"
    edit_perm = "manage_payments" if page == "payment" else "manage_settings"
    lang, deny = _guard(request, admin, view_perm)
    if deny:
        return deny
    svc = SettingsService(session)
    rows = {r.key: r for r in await svc.all_rows()}
    items = _settings_items(rows, category, lang)
    return render_template(
        "settings_page.html",
        _ctx(request, admin, page=page, category=category,
             title=category_title_for(category, lang), items=items,
             can_edit=has_permission(admin.role, edit_perm),
             saved=bool(saved), error=error),
    )


@router.post("/settings/{page}")
async def settings_submit(
    page: str,
    request: Request,
    admin: Admin | None = Depends(get_current_admin_optional),
    session: AsyncSession = Depends(get_session),
):
    category = SETTINGS_PAGES.get(page)
    if category is None:
        return RedirectResponse("/admin/settings/general", status_code=302)
    edit_perm = "manage_payments" if page == "payment" else "manage_settings"
    lang, deny = _guard(request, admin, edit_perm)
    if deny:
        return deny
    form = await request.form()
    values: dict[str, object] = {}
    for d in keys_for_category(category):
        if d.value_type == "bool":
            values[d.key] = d.key in form
        elif d.key in form:
            submitted = str(form[d.key])
            if d.is_secret and submitted == "":
                continue
            values[d.key] = submitted
    svc = SettingsService(session)
    try:
        await svc.update_many(values, actor_type="admin", actor_id=admin.id)
    except ValueError as exc:
        return RedirectResponse(
            f"/admin/settings/{page}?error={quote(str(exc))}", status_code=303
        )
    return RedirectResponse(f"/admin/settings/{page}?saved=1", status_code=303)


# --------------------------------------------------------------------------
# Products
# --------------------------------------------------------------------------
def _parse_int_opt(raw: object) -> int | None:
    text = str(raw or "").strip()
    if text == "":
        return None
    return int(text)


def _product_form_values(form: dict[str, object]) -> dict[str, object]:
    """Coerce the product form into schema kwargs.

    A "license" product never carries an XUI binding, so we drop any stray
    server/inbound values the browser may have posted for it — the type-aware
    form disables those controls but we do not trust the client.
    """
    type_ = str(form.get("type", "")).strip()
    xui_server_id = _parse_int_opt(form.get("xui_server_id"))
    xui_inbound_id = _parse_int_opt(form.get("xui_inbound_id"))
    # A service-action product (renew / add-traffic, Phase 8) modifies an existing
    # service and carries no binding of its own. Only v2ray products can be one.
    applies_to_service = ("applies_to_service" in form) and type_ == "v2ray"
    action_type = str(form.get("action_type", "")).strip() or None
    if not applies_to_service:
        action_type = None
    if type_ != "v2ray" or applies_to_service:
        xui_server_id = None
        xui_inbound_id = None
    return {
        "type": type_,
        "title": str(form.get("title", "")).strip(),
        "description": (str(form.get("description", "")).strip() or None),
        "price": _parse_int_opt(form.get("price")) or 0,
        "duration_days": _parse_int_opt(form.get("duration_days")),
        "traffic_gb": _parse_int_opt(form.get("traffic_gb")),
        "ip_limit": _parse_int_opt(form.get("ip_limit")),
        "xui_server_id": xui_server_id,
        "xui_inbound_id": xui_inbound_id,
        "is_active": "is_active" in form,
        "is_hidden": "is_hidden" in form,
        "sort_order": _parse_int_opt(form.get("sort_order")) or 0,
        "action_type": action_type,
        "applies_to_service": applies_to_service,
    }


async def _product_form_ctx(session: AsyncSession) -> dict[str, object]:
    """Server/inbound options shared by the product create + edit forms."""
    servers = await xui_server_service.list_servers(session, active_only=True)
    return {"xui_servers": servers}


@router.get("/products", response_class=HTMLResponse)
async def products_page(
    request: Request,
    admin: Admin | None = Depends(get_current_admin_optional),
    session: AsyncSession = Depends(get_session),
    saved: int = 0,
    error: str = "",
):
    lang, deny = _guard(request, admin, "manage_products")
    if deny:
        return deny
    products = await product_service.list_for_admin(session)
    return render_template(
        "products.html",
        _ctx(request, admin, products=products, saved=bool(saved), error=error),
    )


@router.get("/products/create", response_class=HTMLResponse)
async def product_new_page(
    request: Request,
    admin: Admin | None = Depends(get_current_admin_optional),
    session: AsyncSession = Depends(get_session),
):
    lang, deny = _guard(request, admin, "manage_products")
    if deny:
        return deny
    return render_template(
        "product_form.html",
        _ctx(request, admin, product=None, product_types=PRODUCT_TYPES, error="",
             inbounds=[], **await _product_form_ctx(session)),
    )


@router.post("/products/create")
async def product_create_submit(
    request: Request,
    admin: Admin | None = Depends(get_current_admin_optional),
    session: AsyncSession = Depends(get_session),
):
    lang, deny = _guard(request, admin, "manage_products")
    if deny:
        return deny
    form = dict(await request.form())
    try:
        data = ProductCreate(**_product_form_values(form))
        await product_service.create(session, data, actor_type="admin", actor_id=admin.id)
        await session.commit()
    except (ValueError, TypeError) as exc:
        return RedirectResponse(f"/admin/products?error={quote(str(exc))}", status_code=303)
    return RedirectResponse("/admin/products?saved=1", status_code=303)


@router.get("/products/{product_id}/edit", response_class=HTMLResponse)
async def product_edit_page(
    product_id: int,
    request: Request,
    admin: Admin | None = Depends(get_current_admin_optional),
    session: AsyncSession = Depends(get_session),
):
    lang, deny = _guard(request, admin, "manage_products")
    if deny:
        return deny
    product = await product_service.get(session, product_id)
    if product is None:
        return RedirectResponse("/admin/products", status_code=302)
    # Pre-populate the inbound dropdown with the bound server's inbounds so the
    # current selection renders without JavaScript.
    inbounds: list = []
    if product.xui_server_id:
        inbounds = await xui_server_service.list_inbounds(session, product.xui_server_id)
    return render_template(
        "product_form.html",
        _ctx(request, admin, product=product, product_types=PRODUCT_TYPES, error="",
             inbounds=inbounds, **await _product_form_ctx(session)),
    )


@router.post("/products/{product_id}/edit")
async def product_edit_submit(
    product_id: int,
    request: Request,
    admin: Admin | None = Depends(get_current_admin_optional),
    session: AsyncSession = Depends(get_session),
):
    lang, deny = _guard(request, admin, "manage_products")
    if deny:
        return deny
    form = dict(await request.form())
    try:
        data = ProductUpdate(**_product_form_values(form))
        product = await product_service.update(
            session, product_id, data, actor_type="admin", actor_id=admin.id
        )
        await session.commit()
    except (ValueError, TypeError) as exc:
        return RedirectResponse(f"/admin/products?error={quote(str(exc))}", status_code=303)
    if product is None:
        return RedirectResponse("/admin/products", status_code=302)
    return RedirectResponse("/admin/products?saved=1", status_code=303)


@router.post("/products/{product_id}/toggle-active")
async def product_toggle_active(
    product_id: int,
    request: Request,
    admin: Admin | None = Depends(get_current_admin_optional),
    session: AsyncSession = Depends(get_session),
):
    lang, deny = _guard(request, admin, "manage_products")
    if deny:
        return deny
    product = await product_service.get(session, product_id)
    if product is not None:
        await product_service.set_active(
            session, product_id, not product.is_active,
            actor_type="admin", actor_id=admin.id,
        )
        await session.commit()
    return RedirectResponse("/admin/products?saved=1", status_code=303)


@router.post("/products/{product_id}/delete-or-hide")
async def product_delete_or_hide(
    product_id: int,
    request: Request,
    admin: Admin | None = Depends(get_current_admin_optional),
    session: AsyncSession = Depends(get_session),
):
    """Soft-delete: hide the product (no destructive delete in this phase)."""
    lang, deny = _guard(request, admin, "manage_products")
    if deny:
        return deny
    product = await product_service.get(session, product_id)
    if product is not None:
        await product_service.set_hidden(
            session, product_id, not product.is_hidden,
            actor_type="admin", actor_id=admin.id,
        )
        await session.commit()
    return RedirectResponse("/admin/products?saved=1", status_code=303)


# --------------------------------------------------------------------------
# Audit logs
# --------------------------------------------------------------------------
@router.get("/audit-logs", response_class=HTMLResponse)
async def audit_logs_page(
    request: Request,
    admin: Admin | None = Depends(get_current_admin_optional),
    session: AsyncSession = Depends(get_session),
    scope: str = "",
):
    lang, deny = _guard(request, admin, "view_audit_log")
    if deny:
        return deny
    logs = await audit_service.list_recent(session, limit=200)
    if scope == "admin":
        logs = [r for r in logs if r.actor_type == "admin"]
    return render_template(
        "audit_logs.html",
        _ctx(request, admin, logs=logs, scope=scope),
    )


# --------------------------------------------------------------------------
# V2Ray / 3X-UI servers + inbounds (manage_xui)
#
# Foundation only: manage panel records and their inbounds so V2Ray products can
# be bound to a specific server+inbound. Live connectivity (test / sync) is
# best-effort and never blocks CRUD. Credentials are never rendered.
# --------------------------------------------------------------------------
def _server_form_values(form: dict[str, object]) -> dict[str, object]:
    """Extract server form fields. Password/token empty means 'keep existing'."""
    return {
        "name": str(form.get("name", "")).strip(),
        "base_url": str(form.get("base_url", "")).strip(),
        "username": str(form.get("username", "")).strip() or None,
        "password": str(form.get("password", "")) or None,
        "api_token": str(form.get("api_token", "")).strip() or None,
        "is_active": "is_active" in form,
    }


# --- old /admin/servers routes now live at /admin/xui-servers ---------------
@router.get("/servers")
async def servers_legacy_redirect():
    return RedirectResponse("/admin/xui-servers", status_code=301)


@router.get("/xui-servers", response_class=HTMLResponse)
async def xui_servers_page(
    request: Request,
    admin: Admin | None = Depends(get_current_admin_optional),
    session: AsyncSession = Depends(get_session),
    saved: int = 0,
    error: str = "",
    tested: str = "",
    synced: str = "",
):
    lang, deny = _guard(request, admin, "manage_xui")
    if deny:
        return deny
    servers = await xui_server_service.list_servers(session)
    counts = await xui_server_service.inbound_counts(session)
    return render_template(
        "xui_servers.html",
        _ctx(request, admin, servers=servers, counts=counts,
             saved=bool(saved), error=error, tested=tested, synced=synced),
    )


@router.get("/xui-servers/create", response_class=HTMLResponse)
async def xui_server_new_page(
    request: Request,
    admin: Admin | None = Depends(get_current_admin_optional),
):
    lang, deny = _guard(request, admin, "manage_xui")
    if deny:
        return deny
    return render_template(
        "xui_server_form.html",
        _ctx(request, admin, server=None, error=""),
    )


@router.post("/xui-servers/create")
async def xui_server_create_submit(
    request: Request,
    admin: Admin | None = Depends(get_current_admin_optional),
    session: AsyncSession = Depends(get_session),
):
    lang, deny = _guard(request, admin, "manage_xui")
    if deny:
        return deny
    values = _server_form_values(dict(await request.form()))
    try:
        await xui_server_service.create_server(
            session, actor_id=admin.id, **values
        )
        await session.commit()
    except (ValueError, TypeError) as exc:
        return RedirectResponse(
            f"/admin/xui-servers?error={quote(str(exc))}", status_code=303
        )
    return RedirectResponse("/admin/xui-servers?saved=1", status_code=303)


@router.get("/xui-servers/{server_id}/edit", response_class=HTMLResponse)
async def xui_server_edit_page(
    server_id: int,
    request: Request,
    admin: Admin | None = Depends(get_current_admin_optional),
    session: AsyncSession = Depends(get_session),
):
    lang, deny = _guard(request, admin, "manage_xui")
    if deny:
        return deny
    server = await xui_server_service.get_server(session, server_id)
    if server is None:
        return RedirectResponse("/admin/xui-servers", status_code=302)
    return render_template(
        "xui_server_form.html",
        _ctx(request, admin, server=server, error=""),
    )


@router.post("/xui-servers/{server_id}/edit")
async def xui_server_edit_submit(
    server_id: int,
    request: Request,
    admin: Admin | None = Depends(get_current_admin_optional),
    session: AsyncSession = Depends(get_session),
):
    lang, deny = _guard(request, admin, "manage_xui")
    if deny:
        return deny
    values = _server_form_values(dict(await request.form()))
    try:
        server = await xui_server_service.update_server(
            session, server_id, actor_id=admin.id, **values
        )
        await session.commit()
    except (ValueError, TypeError) as exc:
        return RedirectResponse(
            f"/admin/xui-servers?error={quote(str(exc))}", status_code=303
        )
    if server is None:
        return RedirectResponse("/admin/xui-servers", status_code=302)
    return RedirectResponse("/admin/xui-servers?saved=1", status_code=303)


@router.post("/xui-servers/{server_id}/deactivate")
async def xui_server_deactivate(
    server_id: int,
    request: Request,
    admin: Admin | None = Depends(get_current_admin_optional),
    session: AsyncSession = Depends(get_session),
):
    lang, deny = _guard(request, admin, "manage_xui")
    if deny:
        return deny
    await xui_server_service.delete_or_deactivate_server(
        session, server_id, actor_id=admin.id
    )
    await session.commit()
    return RedirectResponse("/admin/xui-servers?saved=1", status_code=303)


@router.post("/xui-servers/{server_id}/test")
async def xui_server_test(
    server_id: int,
    request: Request,
    admin: Admin | None = Depends(get_current_admin_optional),
    session: AsyncSession = Depends(get_session),
):
    lang, deny = _guard(request, admin, "manage_xui")
    if deny:
        return deny
    result = await xui_server_service.test_connection(session, server_id, actor_id=admin.id)
    await session.commit()
    message = str(result.get("message", ""))
    return RedirectResponse(
        f"/admin/xui-servers?tested={quote(message)}", status_code=303
    )


@router.post("/xui-servers/{server_id}/sync-inbounds")
async def xui_server_sync(
    server_id: int,
    request: Request,
    admin: Admin | None = Depends(get_current_admin_optional),
    session: AsyncSession = Depends(get_session),
):
    lang, deny = _guard(request, admin, "manage_xui")
    if deny:
        return deny
    result = await xui_server_service.sync_inbounds(session, server_id, actor_id=admin.id)
    await session.commit()
    if not result.get("ok"):
        return RedirectResponse(
            f"/admin/xui-servers?error={quote(str(result.get('message', '')))}",
            status_code=303,
        )
    return RedirectResponse(
        f"/admin/xui-servers?synced={result.get('count', 0)}", status_code=303
    )


# --- inbounds ----------------------------------------------------------------
@router.get("/xui-inbounds", response_class=HTMLResponse)
async def xui_inbounds_overview(
    request: Request,
    admin: Admin | None = Depends(get_current_admin_optional),
    session: AsyncSession = Depends(get_session),
):
    """Read-only overview of every inbound grouped by server."""
    lang, deny = _guard(request, admin, "manage_xui")
    if deny:
        return deny
    servers = await xui_server_service.list_servers(session)
    groups = []
    for s in servers:
        groups.append(
            {"server": s, "inbounds": await xui_server_service.list_inbounds(session, s.id)}
        )
    return render_template(
        "xui_inbounds_overview.html",
        _ctx(request, admin, groups=groups),
    )


@router.get("/xui-servers/{server_id}/inbounds", response_class=HTMLResponse)
async def xui_server_inbounds_page(
    server_id: int,
    request: Request,
    admin: Admin | None = Depends(get_current_admin_optional),
    session: AsyncSession = Depends(get_session),
    saved: int = 0,
    error: str = "",
):
    lang, deny = _guard(request, admin, "manage_xui")
    if deny:
        return deny
    server = await xui_server_service.get_server(session, server_id)
    if server is None:
        return RedirectResponse("/admin/xui-servers", status_code=302)
    inbounds = await xui_server_service.list_inbounds(session, server_id)
    return render_template(
        "xui_inbounds.html",
        _ctx(request, admin, server=server, inbounds=inbounds,
             saved=bool(saved), error=error),
    )


@router.get("/xui-servers/{server_id}/inbounds/create", response_class=HTMLResponse)
async def xui_inbound_new_page(
    server_id: int,
    request: Request,
    admin: Admin | None = Depends(get_current_admin_optional),
    session: AsyncSession = Depends(get_session),
):
    lang, deny = _guard(request, admin, "manage_xui")
    if deny:
        return deny
    server = await xui_server_service.get_server(session, server_id)
    if server is None:
        return RedirectResponse("/admin/xui-servers", status_code=302)
    return render_template(
        "xui_inbound_form.html",
        _ctx(request, admin, server=server, inbound=None, error=""),
    )


@router.post("/xui-servers/{server_id}/inbounds/create")
async def xui_inbound_create_submit(
    server_id: int,
    request: Request,
    admin: Admin | None = Depends(get_current_admin_optional),
    session: AsyncSession = Depends(get_session),
):
    lang, deny = _guard(request, admin, "manage_xui")
    if deny:
        return deny
    form = dict(await request.form())
    try:
        inbound_id = _parse_int_opt(form.get("inbound_id"))
        if inbound_id is None:
            raise ValueError("inbound_id is required")
        await xui_server_service.create_inbound(
            session, server_id, inbound_id,
            remark=str(form.get("remark", "")).strip() or None,
            protocol=str(form.get("protocol", "")).strip() or None,
            port=_parse_int_opt(form.get("port")),
            network=str(form.get("network", "")).strip() or None,
            security=str(form.get("security", "")).strip() or None,
            is_active="is_active" in form,
            actor_id=admin.id,
        )
        await session.commit()
    except (ValueError, TypeError) as exc:
        return RedirectResponse(
            f"/admin/xui-servers/{server_id}/inbounds?error={quote(str(exc))}",
            status_code=303,
        )
    return RedirectResponse(
        f"/admin/xui-servers/{server_id}/inbounds?saved=1", status_code=303
    )


@router.get("/xui-inbounds/{inbound_record_id}/edit", response_class=HTMLResponse)
async def xui_inbound_edit_page(
    inbound_record_id: int,
    request: Request,
    admin: Admin | None = Depends(get_current_admin_optional),
    session: AsyncSession = Depends(get_session),
):
    lang, deny = _guard(request, admin, "manage_xui")
    if deny:
        return deny
    inbound = await xui_server_service.get_inbound(session, inbound_record_id)
    if inbound is None:
        return RedirectResponse("/admin/xui-servers", status_code=302)
    server = await xui_server_service.get_server(session, inbound.server_id)
    return render_template(
        "xui_inbound_form.html",
        _ctx(request, admin, server=server, inbound=inbound, error=""),
    )


@router.post("/xui-inbounds/{inbound_record_id}/edit")
async def xui_inbound_edit_submit(
    inbound_record_id: int,
    request: Request,
    admin: Admin | None = Depends(get_current_admin_optional),
    session: AsyncSession = Depends(get_session),
):
    lang, deny = _guard(request, admin, "manage_xui")
    if deny:
        return deny
    inbound = await xui_server_service.get_inbound(session, inbound_record_id)
    if inbound is None:
        return RedirectResponse("/admin/xui-servers", status_code=302)
    server_id = inbound.server_id
    form = dict(await request.form())
    try:
        await xui_server_service.update_inbound(
            session, inbound_record_id,
            inbound_id=_parse_int_opt(form.get("inbound_id")),
            remark=str(form.get("remark", "")).strip() or None,
            protocol=str(form.get("protocol", "")).strip() or None,
            port=_parse_int_opt(form.get("port")),
            network=str(form.get("network", "")).strip() or None,
            security=str(form.get("security", "")).strip() or None,
            is_active="is_active" in form,
            actor_id=admin.id,
        )
        await session.commit()
    except (ValueError, TypeError) as exc:
        return RedirectResponse(
            f"/admin/xui-servers/{server_id}/inbounds?error={quote(str(exc))}",
            status_code=303,
        )
    return RedirectResponse(
        f"/admin/xui-servers/{server_id}/inbounds?saved=1", status_code=303
    )


@router.post("/xui-inbounds/{inbound_record_id}/deactivate")
async def xui_inbound_deactivate(
    inbound_record_id: int,
    request: Request,
    admin: Admin | None = Depends(get_current_admin_optional),
    session: AsyncSession = Depends(get_session),
):
    lang, deny = _guard(request, admin, "manage_xui")
    if deny:
        return deny
    inbound = await xui_server_service.get_inbound(session, inbound_record_id)
    if inbound is None:
        return RedirectResponse("/admin/xui-servers", status_code=302)
    server_id = inbound.server_id
    await xui_server_service.deactivate_inbound(session, inbound_record_id, actor_id=admin.id)
    await session.commit()
    return RedirectResponse(
        f"/admin/xui-servers/{server_id}/inbounds?saved=1", status_code=303
    )


# --- JSON: active inbounds for a server (feeds the product form dropdown) ----
@router.get("/api/xui-servers/{server_id}/inbounds")
async def xui_server_inbounds_json(
    server_id: int,
    request: Request,
    admin: Admin | None = Depends(get_current_admin_optional),
    session: AsyncSession = Depends(get_session),
):
    if admin is None:
        return JSONResponse({"error": "unauthorized"}, status_code=401)
    if not has_permission(admin.role, "manage_products"):
        return JSONResponse({"error": "forbidden"}, status_code=403)
    inbounds = await xui_server_service.list_inbounds(session, server_id, active_only=True)
    return JSONResponse(
        {
            "server_id": server_id,
            "inbounds": [
                {
                    "id": ib.id,
                    "inbound_id": ib.inbound_id,
                    "remark": ib.remark,
                    "protocol": ib.protocol,
                    "port": ib.port,
                }
                for ib in inbounds
            ],
        }
    )


# --------------------------------------------------------------------------
# Orders + receipts (view_payments) — Phase 3/4.
#
# List orders, inspect one, view its receipt, and (Phase 4) run the receipt-review
# quick actions: approve/reject, wallet add/subtract, block/restrict the user.
# --------------------------------------------------------------------------
def _order_action_perms(role: object) -> dict:
    return {
        "can_process": has_permission(role, "process_payments"),
        "can_adjust_wallet": has_permission(role, "adjust_wallet"),
        "can_block": has_permission(role, "block_users"),
        "can_restrict": has_permission(role, "restrict_users"),
        "can_view_users": has_permission(role, "view_users"),
        "can_refund": has_permission(role, "refund_payments"),
    }


def _order_row(order, lang: str) -> dict:
    """A flat, template-ready view of an order + its payment."""
    payment = order.payment
    product = order.product
    user = order.user
    return {
        "id": order.id,
        "number": order.order_number,
        "user_label": (user.username or (user.telegram_id and str(user.telegram_id))
                       or f"#{user.id}") if user else "—",
        "product_title": product.title if product else "—",
        "product_type": product.type if product else "—",
        "amount": order.final_amount,
        "method": order.payment_method,
        "order_status": order.status,
        "order_status_label": order_status_label(order.status, lang),
        "payment_status": payment.status if payment else None,
        "payment_status_label": payment_status_label(payment.status, lang) if payment else "—",
        "created_at": order.created_at,
        "submitted_at": payment.submitted_at if payment else None,
        "has_receipt": bool(payment and payment.receipt_path),
        "payment_id": payment.id if payment else None,
    }


@router.get("/orders", response_class=HTMLResponse)
async def orders_page(
    request: Request,
    admin: Admin | None = Depends(get_current_admin_optional),
    session: AsyncSession = Depends(get_session),
):
    lang, deny = _guard(request, admin, "view_payments")
    if deny:
        return deny
    orders = await order_service.list_all_orders(session)
    rows = [_order_row(o, lang) for o in orders]
    return render_template(
        "orders.html",
        _ctx(request, admin, rows=rows, pending_only=False,
             perms=_order_action_perms(admin.role)),
    )


@router.get("/orders/pending-receipts", response_class=HTMLResponse)
async def orders_pending_page(
    request: Request,
    admin: Admin | None = Depends(get_current_admin_optional),
    session: AsyncSession = Depends(get_session),
):
    lang, deny = _guard(request, admin, "view_payments")
    if deny:
        return deny
    orders = await order_service.list_pending_receipt_orders(session)
    rows = [_order_row(o, lang) for o in orders]
    return render_template(
        "orders.html",
        _ctx(request, admin, rows=rows, pending_only=True,
             perms=_order_action_perms(admin.role)),
    )


@router.get("/orders/{order_id}", response_class=HTMLResponse)
async def order_detail_page(
    order_id: int,
    request: Request,
    admin: Admin | None = Depends(get_current_admin_optional),
    session: AsyncSession = Depends(get_session),
    saved: str = "",
    error: str = "",
):
    lang, deny = _guard(request, admin, "view_payments")
    if deny:
        return deny
    order = await order_service.get_order(session, order_id)
    if order is None:
        return RedirectResponse("/admin/orders", status_code=302)
    # Related audit rows for this order (best-effort; small list).
    logs = [
        r for r in await audit_service.list_recent(session, limit=500)
        if (r.target_type == "order" and str(r.target_id) == str(order.id))
        or (r.target_type == "payment" and order.payment and str(r.target_id) == str(order.payment.id))
    ]
    wallet_txns = []
    if order.user:
        wallet_txns = await user_service.list_wallet_transactions(
            session, user_id=order.user_id, limit=10
        )
    return render_template(
        "order_detail.html",
        _ctx(request, admin, order=order, payment=order.payment, product=order.product,
             user=order.user, row=_order_row(order, lang), logs=logs,
             wallet_txns=wallet_txns, perms=_order_action_perms(admin.role),
             saved=saved, error=error),
    )


# --- receipt-review quick actions (Phase 4) ---------------------------------
def _order_back(order_id: int, *, saved: str = "", error: str = "") -> RedirectResponse:
    q = f"saved={quote(saved)}" if saved else f"error={quote(error)}"
    return RedirectResponse(f"/admin/orders/{order_id}?{q}", status_code=303)


@router.post("/orders/{order_id}/approve")
async def order_approve(
    order_id: int,
    request: Request,
    admin: Admin | None = Depends(get_current_admin_optional),
    session: AsyncSession = Depends(get_session),
):
    lang, deny = _guard(request, admin, "process_payments")
    if deny:
        return deny
    try:
        result = await payment_service.approve_payment(session, order_id, admin_id=admin.id)
        await session.commit()
    except (ValueError, payment_service.ReceiptError) as exc:
        return _order_back(order_id, error=str(exc))
    delivered = result.get("delivery", {}).get("delivered")
    return _order_back(order_id, saved="approved" if delivered else "approved_undelivered")


@router.post("/orders/{order_id}/reject")
async def order_reject(
    order_id: int,
    request: Request,
    admin: Admin | None = Depends(get_current_admin_optional),
    session: AsyncSession = Depends(get_session),
):
    lang, deny = _guard(request, admin, "process_payments")
    if deny:
        return deny
    form = dict(await request.form())
    reason = str(form.get("reason", "")).strip()
    try:
        await payment_service.reject_payment(session, order_id, admin_id=admin.id, reason=reason)
        await session.commit()
    except (ValueError, payment_service.ReceiptError) as exc:
        return _order_back(order_id, error=str(exc))
    return _order_back(order_id, saved="rejected")


async def _order_user_id(session: AsyncSession, order_id: int) -> tuple[int | None, int | None]:
    order = await order_service.get_order(session, order_id)
    return (order.id, order.user_id) if order else (None, None)


@router.post("/orders/{order_id}/add-balance")
async def order_add_balance(
    order_id: int,
    request: Request,
    admin: Admin | None = Depends(get_current_admin_optional),
    session: AsyncSession = Depends(get_session),
):
    lang, deny = _guard(request, admin, "adjust_wallet")
    if deny:
        return deny
    _oid, user_id = await _order_user_id(session, order_id)
    if user_id is None:
        return RedirectResponse("/admin/orders", status_code=302)
    form = dict(await request.form())
    try:
        amount = int(str(form.get("amount", "0")).strip() or "0")
        await wallet_service.add_balance(
            session, user_id, amount, admin_id=admin.id,
            reason=str(form.get("reason", "")), ip_address=_client_ip(request),
        )
        await audit_service.log(
            session, actor_type="admin", actor_id=admin.id,
            action="admin_wallet_added_from_receipt_review", target_type="user",
            target_id=user_id, new=str(amount),
            meta=f"order_id={order_id} amount={amount}",
        )
        await session.commit()
    except ValueError as exc:
        return _order_back(order_id, error=str(exc))
    return _order_back(order_id, saved="wallet_added")


@router.post("/orders/{order_id}/subtract-balance")
async def order_subtract_balance(
    order_id: int,
    request: Request,
    admin: Admin | None = Depends(get_current_admin_optional),
    session: AsyncSession = Depends(get_session),
):
    lang, deny = _guard(request, admin, "adjust_wallet")
    if deny:
        return deny
    _oid, user_id = await _order_user_id(session, order_id)
    if user_id is None:
        return RedirectResponse("/admin/orders", status_code=302)
    form = dict(await request.form())
    try:
        amount = int(str(form.get("amount", "0")).strip() or "0")
        await wallet_service.subtract_balance(
            session, user_id, amount, admin_id=admin.id,
            reason=str(form.get("reason", "")), ip_address=_client_ip(request),
        )
        await audit_service.log(
            session, actor_type="admin", actor_id=admin.id,
            action="admin_wallet_subtracted_from_receipt_review", target_type="user",
            target_id=user_id, new=str(amount),
            meta=f"order_id={order_id} amount={amount}",
        )
        await session.commit()
    except ValueError as exc:
        return _order_back(order_id, error=str(exc))
    return _order_back(order_id, saved="wallet_subtracted")


@router.post("/orders/{order_id}/block-user")
async def order_block_user(
    order_id: int,
    request: Request,
    admin: Admin | None = Depends(get_current_admin_optional),
    session: AsyncSession = Depends(get_session),
):
    lang, deny = _guard(request, admin, "block_users")
    if deny:
        return deny
    _oid, user_id = await _order_user_id(session, order_id)
    if user_id is None:
        return RedirectResponse("/admin/orders", status_code=302)
    form = dict(await request.form())
    reason = str(form.get("reason", "")).strip() or None
    await user_service.admin_set_blocked(
        session, user_id, True, actor_id=admin.id, ip_address=_client_ip(request)
    )
    if reason:
        await user_service.update_admin_note(session, user_id, reason, actor_id=admin.id)
    await audit_service.log(
        session, actor_type="admin", actor_id=admin.id,
        action="user_blocked_from_receipt_review", target_type="user",
        target_id=user_id, meta=f"order_id={order_id} reason={reason or ''}",
    )
    await session.commit()
    return _order_back(order_id, saved="user_blocked")


@router.post("/orders/{order_id}/restrict-user")
async def order_restrict_user(
    order_id: int,
    request: Request,
    admin: Admin | None = Depends(get_current_admin_optional),
    session: AsyncSession = Depends(get_session),
):
    lang, deny = _guard(request, admin, "restrict_users")
    if deny:
        return deny
    _oid, user_id = await _order_user_id(session, order_id)
    if user_id is None:
        return RedirectResponse("/admin/orders", status_code=302)
    form = dict(await request.form())
    reason = str(form.get("reason", "")).strip()
    if not reason:
        return _order_back(order_id, error="a reason is required")
    await user_service.set_restricted(
        session, user_id, True, reason=reason, actor_id=admin.id,
        ip_address=_client_ip(request),
    )
    await audit_service.log(
        session, actor_type="admin", actor_id=admin.id,
        action="user_restricted_from_receipt_review", target_type="user",
        target_id=user_id, meta=f"order_id={order_id} reason={reason}",
    )
    await session.commit()
    return _order_back(order_id, saved="user_restricted")


@router.post("/orders/{order_id}/unrestrict-user")
async def order_unrestrict_user(
    order_id: int,
    request: Request,
    admin: Admin | None = Depends(get_current_admin_optional),
    session: AsyncSession = Depends(get_session),
):
    lang, deny = _guard(request, admin, "restrict_users")
    if deny:
        return deny
    _oid, user_id = await _order_user_id(session, order_id)
    if user_id is None:
        return RedirectResponse("/admin/orders", status_code=302)
    await user_service.set_restricted(
        session, user_id, False, actor_id=admin.id, ip_address=_client_ip(request)
    )
    await audit_service.log(
        session, actor_type="admin", actor_id=admin.id, action="user_unrestricted",
        target_type="user", target_id=user_id, meta=f"order_id={order_id}",
    )
    await session.commit()
    return _order_back(order_id, saved="user_unrestricted")


@router.get("/receipts/{payment_id}")
async def receipt_file(
    payment_id: int,
    request: Request,
    admin: Admin | None = Depends(get_current_admin_optional),
    session: AsyncSession = Depends(get_session),
):
    """Serve a receipt to authenticated admins only, guarding path traversal."""
    lang, deny = _guard(request, admin, "view_payments")
    if deny:
        return deny
    payment = await session.get(Payment, payment_id)
    if payment is None or not payment.receipt_path:
        return HTMLResponse(t("web.receipts.not_found", lang), status_code=404)
    resolved = payment_service.resolve_receipt_path(payment.receipt_path)
    if resolved is None:
        return HTMLResponse(t("web.receipts.not_found", lang), status_code=404)
    await audit_service.log(
        session, actor_type="admin", actor_id=admin.id, action="admin_viewed_receipt",
        target_type="payment", target_id=payment.id,
    )
    filename = payment.receipt_original_name or resolved.name
    return FileResponse(
        resolved,
        media_type=payment.receipt_mime_type or "application/octet-stream",
        filename=filename,
        content_disposition_type="inline",
        headers={"X-Content-Type-Options": "nosniff", "Cache-Control": "private, no-store"},
    )


# --------------------------------------------------------------------------
# Licenses (Phase 5) — stock, import, sold, low-stock, detail + actions.
#
# List pages never show a password; the detail page reveals it only to admins
# with `view_license_secrets`. Import needs `import_licenses`; block/mark-broken/
# redeliver/replace need `manage_licenses`.
# --------------------------------------------------------------------------
async def _license_products(session: AsyncSession):
    products = await product_service.list_for_admin(session)
    return [p for p in products if p.type == "license"]


@router.get("/licenses", response_class=HTMLResponse)
async def licenses_page(
    request: Request,
    admin: Admin | None = Depends(get_current_admin_optional),
    session: AsyncSession = Depends(get_session),
    product_id: int = 0,
    status: str = "",
    saved: str = "",
    error: str = "",
):
    lang, deny = _guard(request, admin, "view_licenses")
    if deny:
        return deny
    licenses = await license_service.list_licenses(
        session, product_id=(product_id or None), status=(status or None), limit=200
    )
    return render_template(
        "licenses.html",
        _ctx(request, admin, licenses=licenses, products=await _license_products(session),
             product_id=product_id, status=status, saved=saved, error=error),
    )


@router.get("/licenses/import", response_class=HTMLResponse)
async def license_import_page(
    request: Request,
    admin: Admin | None = Depends(get_current_admin_optional),
    session: AsyncSession = Depends(get_session),
):
    lang, deny = _guard(request, admin, "import_licenses")
    if deny:
        return deny
    return render_template(
        "license_import.html",
        _ctx(request, admin, products=await _license_products(session), result=None, error=""),
    )


@router.post("/licenses/import", response_class=HTMLResponse)
async def license_import_submit(
    request: Request,
    admin: Admin | None = Depends(get_current_admin_optional),
    session: AsyncSession = Depends(get_session),
):
    lang, deny = _guard(request, admin, "import_licenses")
    if deny:
        return deny
    form = await request.form()
    product_id = _parse_int_opt(form.get("product_id"))
    raw_text = str(form.get("raw_text", "") or "")
    upload = form.get("file")
    if upload is not None and hasattr(upload, "read"):
        data = await upload.read()
        if data:
            raw_text = (raw_text + "\n" + data.decode("utf-8", errors="replace")).strip()
    result = None
    error = ""
    try:
        if not product_id:
            raise ValueError("please choose a license product")
        result = await license_service.bulk_import_licenses(
            session, product_id, raw_text, admin_id=admin.id
        )
        await session.commit()
    except (ValueError, license_service.LicenseError) as exc:
        error = str(exc)
    return render_template(
        "license_import.html",
        _ctx(request, admin, products=await _license_products(session),
             result=result, error=error, product_id=product_id or 0),
    )


@router.get("/licenses/sold", response_class=HTMLResponse)
async def licenses_sold_page(
    request: Request,
    admin: Admin | None = Depends(get_current_admin_optional),
    session: AsyncSession = Depends(get_session),
):
    lang, deny = _guard(request, admin, "view_licenses")
    if deny:
        return deny
    licenses = await license_service.list_sold(session, limit=200)
    return render_template(
        "licenses_sold.html", _ctx(request, admin, licenses=licenses),
    )


@router.get("/licenses/low-stock", response_class=HTMLResponse)
async def licenses_low_stock_page(
    request: Request,
    admin: Admin | None = Depends(get_current_admin_optional),
    session: AsyncSession = Depends(get_session),
):
    lang, deny = _guard(request, admin, "view_licenses")
    if deny:
        return deny
    threshold = await SettingsService(session).get_int("license_low_stock_threshold", 5)
    products = await _license_products(session)
    rows = []
    for p in products:
        avail = await license_service.count_available(session, p.id)
        rows.append({"product": p, "available": avail, "threshold": threshold,
                     "low": avail < threshold})
    return render_template(
        "licenses_low_stock.html",
        _ctx(request, admin, rows=rows, threshold=threshold),
    )


@router.get("/licenses/{license_id}", response_class=HTMLResponse)
async def license_detail_page(
    license_id: int,
    request: Request,
    admin: Admin | None = Depends(get_current_admin_optional),
    session: AsyncSession = Depends(get_session),
    saved: str = "",
    error: str = "",
):
    lang, deny = _guard(request, admin, "view_licenses")
    if deny:
        return deny
    lic = await license_service.get_license(session, license_id)
    if lic is None:
        return RedirectResponse("/admin/licenses", status_code=302)
    return render_template(
        "license_detail.html",
        _ctx(request, admin, lic=lic,
             can_secrets=has_permission(admin.role, "view_license_secrets"),
             can_manage=has_permission(admin.role, "manage_licenses"),
             saved=saved, error=error),
    )


def _license_back(license_id: int, *, saved: str = "", error: str = "") -> RedirectResponse:
    q = f"saved={quote(saved)}" if saved else f"error={quote(error)}"
    return RedirectResponse(f"/admin/licenses/{license_id}?{q}", status_code=303)


@router.post("/licenses/{license_id}/block")
async def license_block(
    license_id: int,
    request: Request,
    admin: Admin | None = Depends(get_current_admin_optional),
    session: AsyncSession = Depends(get_session),
):
    lang, deny = _guard(request, admin, "manage_licenses")
    if deny:
        return deny
    try:
        await license_service.block_license(session, license_id, admin_id=admin.id)
        await session.commit()
    except license_service.LicenseError as exc:
        return _license_back(license_id, error=str(exc))
    return _license_back(license_id, saved="blocked")


@router.post("/licenses/{license_id}/mark-broken")
async def license_mark_broken(
    license_id: int,
    request: Request,
    admin: Admin | None = Depends(get_current_admin_optional),
    session: AsyncSession = Depends(get_session),
):
    lang, deny = _guard(request, admin, "manage_licenses")
    if deny:
        return deny
    form = dict(await request.form())
    reason = str(form.get("reason", "")).strip() or None
    await license_service.mark_license_broken(session, license_id, admin_id=admin.id, reason=reason)
    await session.commit()
    return _license_back(license_id, saved="marked_broken")


@router.post("/licenses/{license_id}/redeliver")
async def license_redeliver(
    license_id: int,
    request: Request,
    admin: Admin | None = Depends(get_current_admin_optional),
    session: AsyncSession = Depends(get_session),
):
    lang, deny = _guard(request, admin, "manage_licenses")
    if deny:
        return deny
    lic = await license_service.get_license(session, license_id)
    if lic is None or not lic.order_id:
        return _license_back(license_id, error="license is not attached to an order")
    result = await license_service.redeliver_license(session, lic.order_id, admin_id=admin.id)
    await session.commit()
    if not result.get("ok"):
        return _license_back(license_id, error="redelivery failed")
    return _license_back(license_id, saved="redelivered")


@router.post("/licenses/{license_id}/replace")
async def license_replace(
    license_id: int,
    request: Request,
    admin: Admin | None = Depends(get_current_admin_optional),
    session: AsyncSession = Depends(get_session),
):
    lang, deny = _guard(request, admin, "manage_licenses")
    if deny:
        return deny
    lic = await license_service.get_license(session, license_id)
    if lic is None or not lic.order_id:
        return _license_back(license_id, error="license is not attached to an order")
    form = dict(await request.form())
    reason = str(form.get("reason", "")).strip() or None
    try:
        await license_service.replace_license(
            session, lic.order_id, admin_id=admin.id, reason=reason
        )
        await session.commit()
    except license_service.LicenseError as exc:
        return _license_back(license_id, error=str(exc))
    return _license_back(license_id, saved="replaced")


# --------------------------------------------------------------------------
# V2Ray services (Phase 6) — provisioned 3X-UI clients.
#
# List/detail need `view_services`; refresh-usage is `view_services` (support may
# refresh); disable/enable/delete/reset/retry need `manage_services`. No XUI
# credential (panel password/token) is ever rendered.
# --------------------------------------------------------------------------
_GB = 1024 ** 3


def _mask_uuid(u: str | None) -> str:
    if not u:
        return "—"
    return u if len(u) <= 8 else f"{u[:4]}…{u[-4:]}"


def _gb_disp(byte_count: int | None) -> str:
    b = int(byte_count or 0)
    if b <= 0:
        return "∞"
    return f"{b / _GB:.2f} GB"


def _v2ray_row(svc, lang: str) -> dict:
    user = svc.user
    return {
        "id": svc.id,
        "user_label": (user.username or (user.telegram_id and str(user.telegram_id))
                       or f"#{svc.user_id}") if user else "—",
        "product_title": svc.product.title if svc.product else "—",
        "server_name": svc.xui_server.name if svc.xui_server else "—",
        "inbound": svc.xui_inbound.inbound_id if svc.xui_inbound else "—",
        "client_email": svc.client_email,
        "status": svc.status,
        "used_disp": _gb_disp(svc.used_gb),
        "total_disp": _gb_disp(svc.total_gb),
        "expire_at": svc.expire_at,
        "created_at": svc.created_at,
    }


@router.get("/v2ray-services", response_class=HTMLResponse)
async def v2ray_services_page(
    request: Request,
    admin: Admin | None = Depends(get_current_admin_optional),
    session: AsyncSession = Depends(get_session),
    status: str = "",
    server_id: int = 0,
    product_id: int = 0,
    saved: str = "",
    error: str = "",
):
    lang, deny = _guard(request, admin, "view_services")
    if deny:
        return deny
    services = await v2ray_service.list_services(
        session, status=(status or None), server_id=(server_id or None),
        product_id=(product_id or None), limit=200,
    )
    rows = [_v2ray_row(s, lang) for s in services]
    counts = await v2ray_service.count_by_status(session)
    return render_template(
        "v2ray_services.html",
        _ctx(request, admin, rows=rows, counts=counts, status=status,
             saved=saved, error=error),
    )


@router.get("/v2ray-services/{service_id}", response_class=HTMLResponse)
async def v2ray_service_detail_page(
    service_id: int,
    request: Request,
    admin: Admin | None = Depends(get_current_admin_optional),
    session: AsyncSession = Depends(get_session),
    saved: str = "",
    error: str = "",
):
    lang, deny = _guard(request, admin, "view_services")
    if deny:
        return deny
    svc = await v2ray_service.get_service(session, service_id)
    if svc is None:
        return RedirectResponse("/admin/v2ray-services", status_code=302)
    logs = [
        r for r in await audit_service.list_recent(session, limit=500)
        if (r.target_type == "v2ray_service" and str(r.target_id) == str(svc.id))
        or (r.target_type == "order" and str(r.target_id) == str(svc.order_id))
    ]
    rem_bytes = v2ray_lifecycle_service.remaining_bytes(svc)
    rem_days = v2ray_lifecycle_service.remaining_days(svc)
    return render_template(
        "v2ray_service_detail.html",
        _ctx(request, admin, svc=svc, masked_uuid=_mask_uuid(svc.client_uuid),
             used_disp=_gb_disp(svc.used_gb), total_disp=_gb_disp(svc.total_gb),
             remaining_disp=("∞" if rem_bytes is None else _gb_disp(rem_bytes)),
             remaining_days=rem_days,
             logs=logs, can_manage=has_permission(admin.role, "manage_services"),
             saved=saved, error=error),
    )


def _service_back(service_id: int, *, saved: str = "", error: str = "") -> RedirectResponse:
    q = f"saved={quote(saved)}" if saved else f"error={quote(error)}"
    return RedirectResponse(f"/admin/v2ray-services/{service_id}?{q}", status_code=303)


@router.post("/v2ray-services/{service_id}/refresh-usage")
async def v2ray_refresh_usage(
    service_id: int,
    request: Request,
    admin: Admin | None = Depends(get_current_admin_optional),
    session: AsyncSession = Depends(get_session),
):
    lang, deny = _guard(request, admin, "view_services")
    if deny:
        return deny
    svc = await v2ray_service.refresh_service_usage(session, service_id, actor_id=admin.id)
    if svc is None:
        return RedirectResponse("/admin/v2ray-services", status_code=302)
    return _service_back(service_id, saved="refreshed")


def _service_action(perm: str, saved: str):
    """Build a POST handler that runs one manage-services action + flash."""
    async def handler(
        service_id: int,
        request: Request,
        admin: Admin | None = Depends(get_current_admin_optional),
        session: AsyncSession = Depends(get_session),
    ):
        lang, deny = _guard(request, admin, perm)
        if deny:
            return deny
        fn = getattr(v2ray_service, {
            "disabled": "disable_service",
            "enabled": "enable_service",
            "deleted": "delete_service",
            "reset": "reset_service_traffic",
        }[saved])
        try:
            await fn(session, service_id, actor_id=admin.id)
            await session.commit()
        except v2ray_service.V2RayError as exc:
            return _service_back(service_id, error=str(exc))
        return _service_back(service_id, saved=saved)
    return handler


for _saved in ("disabled", "enabled", "deleted", "reset"):
    _path_seg = {"disabled": "disable", "enabled": "enable",
                 "deleted": "delete", "reset": "reset-traffic"}[_saved]
    router.add_api_route(
        f"/v2ray-services/{{service_id}}/{_path_seg}",
        _service_action("manage_services", _saved),
        methods=["POST"], include_in_schema=False,
    )


@router.post("/v2ray-services/{service_id}/renew")
async def v2ray_renew(
    service_id: int,
    request: Request,
    days: int = Form(...),
    admin: Admin | None = Depends(get_current_admin_optional),
    session: AsyncSession = Depends(get_session),
):
    """Admin renewal (Phase 8): extend the service by `days` and re-enable it."""
    lang, deny = _guard(request, admin, "manage_services")
    if deny:
        return deny
    try:
        await v2ray_lifecycle_service.renew_service(
            session, service_id, duration_days=int(days), actor_id=admin.id)
        await session.commit()
    except v2ray_service.V2RayError as exc:
        return _service_back(service_id, error=str(exc))
    return _service_back(service_id, saved="renewed")


@router.post("/v2ray-services/{service_id}/add-traffic")
async def v2ray_add_traffic(
    service_id: int,
    request: Request,
    gb: int = Form(...),
    admin: Admin | None = Depends(get_current_admin_optional),
    session: AsyncSession = Depends(get_session),
):
    """Admin add-traffic (Phase 8): grow the service quota by `gb` and clear over-quota."""
    lang, deny = _guard(request, admin, "manage_services")
    if deny:
        return deny
    try:
        await v2ray_lifecycle_service.add_traffic(
            session, service_id, traffic_gb=int(gb), actor_id=admin.id)
        await session.commit()
    except v2ray_service.V2RayError as exc:
        return _service_back(service_id, error=str(exc))
    return _service_back(service_id, saved="traffic_added")


@router.post("/orders/{order_id}/retry-v2ray-provisioning")
async def order_retry_v2ray(
    order_id: int,
    request: Request,
    admin: Admin | None = Depends(get_current_admin_optional),
    session: AsyncSession = Depends(get_session),
):
    lang, deny = _guard(request, admin, "manage_services")
    if deny:
        return deny
    # A renew/add-traffic order retries through the lifecycle path; a plain
    # new-service order retries through provisioning.
    order = await order_service.get_order(session, order_id)
    is_action = order is not None and order.action_type in ("renew_service", "add_traffic")
    try:
        if is_action:
            result = await v2ray_lifecycle_service.retry_action_for_order(
                session, order_id, actor_id=admin.id
            )
        else:
            result = await v2ray_service.retry_failed_provisioning(
                session, order_id, actor_id=admin.id
            )
    except v2ray_service.V2RayError as exc:
        return _order_back(order_id, error=str(exc))
    if result.get("ok"):
        return _order_back(order_id, saved="approved")
    return _order_back(order_id, error=f"v2ray:{result.get('reason', 'failed')}")


# --------------------------------------------------------------------------
# Wallet (Phase 7) — top-up requests, approve/reject, transactions, refund.
#
# List/detail/transactions need `view_wallet_topups`; approve/reject need
# `manage_wallet_topups`; refund needs `refund_payments`. Top-up receipts are
# served to authenticated admins only, guarding path traversal.
# --------------------------------------------------------------------------
def _topup_row(topup, lang: str) -> dict:
    user = topup.user
    return {
        "id": topup.id,
        "user_label": (user.username or (user.telegram_id and str(user.telegram_id))
                       or f"#{topup.user_id}") if user else f"#{topup.user_id}",
        "amount": topup.amount,
        "status": topup.status,
        "status_label": topup_status_label(topup.status, lang),
        "submitted_at": topup.submitted_at,
        "admin_id": topup.admin_id,
        "has_receipt": bool(topup.receipt_path),
    }


@router.get("/wallet/topups/pending", response_class=HTMLResponse)
async def wallet_topups_pending_page(
    request: Request,
    admin: Admin | None = Depends(get_current_admin_optional),
    session: AsyncSession = Depends(get_session),
):
    lang, deny = _guard(request, admin, "view_wallet_topups")
    if deny:
        return deny
    topups = await wallet_service.list_pending_topups(session, limit=200)
    rows = [_topup_row(t, lang) for t in topups]
    return render_template(
        "wallet_topups.html",
        _ctx(request, admin, rows=rows, pending_only=True, status="", saved="", error=""),
    )


@router.get("/wallet/topups", response_class=HTMLResponse)
async def wallet_topups_page(
    request: Request,
    admin: Admin | None = Depends(get_current_admin_optional),
    session: AsyncSession = Depends(get_session),
    status: str = "",
    saved: str = "",
    error: str = "",
):
    lang, deny = _guard(request, admin, "view_wallet_topups")
    if deny:
        return deny
    topups = await wallet_service.list_topups(session, status=(status or None), limit=200)
    rows = [_topup_row(t, lang) for t in topups]
    return render_template(
        "wallet_topups.html",
        _ctx(request, admin, rows=rows, pending_only=False, status=status,
             saved=saved, error=error),
    )


@router.get("/wallet/transactions", response_class=HTMLResponse)
async def wallet_transactions_all_page(
    request: Request,
    admin: Admin | None = Depends(get_current_admin_optional),
    session: AsyncSession = Depends(get_session),
):
    lang, deny = _guard(request, admin, "view_wallet_topups")
    if deny:
        return deny
    txns = await user_service.list_wallet_transactions(session, limit=200)
    labels: dict[int, str] = {}
    for uid in {t.user_id for t in txns}:
        u = await user_service.get_by_id(session, uid)
        if u is not None:
            labels[uid] = u.username or (u.telegram_id and str(u.telegram_id)) or f"#{u.id}"
    rows = [{
        "user_label": labels.get(t.user_id, f"#{t.user_id}"),
        "type_label": wallet_tx_type_label(t.type, lang),
        "amount": t.amount,
        "balance_after": t.balance_after,
        "reason": t.reason,
        "created_at": t.created_at,
    } for t in txns]
    return render_template(
        "wallet_all_transactions.html", _ctx(request, admin, rows=rows),
    )


@router.get("/wallet/topups/{topup_id}", response_class=HTMLResponse)
async def wallet_topup_detail_page(
    topup_id: int,
    request: Request,
    admin: Admin | None = Depends(get_current_admin_optional),
    session: AsyncSession = Depends(get_session),
    saved: str = "",
    error: str = "",
):
    lang, deny = _guard(request, admin, "view_wallet_topups")
    if deny:
        return deny
    topup = await wallet_service.get_topup(session, topup_id)
    if topup is None:
        return RedirectResponse("/admin/wallet/topups", status_code=302)
    return render_template(
        "wallet_topup_detail.html",
        _ctx(request, admin, topup=topup, status_label=topup_status_label(topup.status, lang),
             can_manage=has_permission(admin.role, "manage_wallet_topups"),
             saved=saved, error=error),
    )


def _topup_back(topup_id: int, *, saved: str = "", error: str = "") -> RedirectResponse:
    q = f"saved={quote(saved)}" if saved else f"error={quote(error)}"
    return RedirectResponse(f"/admin/wallet/topups/{topup_id}?{q}", status_code=303)


@router.post("/wallet/topups/{topup_id}/approve")
async def wallet_topup_approve(
    topup_id: int,
    request: Request,
    admin: Admin | None = Depends(get_current_admin_optional),
    session: AsyncSession = Depends(get_session),
):
    lang, deny = _guard(request, admin, "manage_wallet_topups")
    if deny:
        return deny
    try:
        await wallet_service.approve_topup(session, topup_id, admin_id=admin.id)
        await session.commit()
    except wallet_service.WalletError as exc:
        return _topup_back(topup_id, error=str(exc))
    return _topup_back(topup_id, saved="approved")


@router.post("/wallet/topups/{topup_id}/reject")
async def wallet_topup_reject(
    topup_id: int,
    request: Request,
    admin: Admin | None = Depends(get_current_admin_optional),
    session: AsyncSession = Depends(get_session),
):
    lang, deny = _guard(request, admin, "manage_wallet_topups")
    if deny:
        return deny
    form = dict(await request.form())
    reason = str(form.get("reason", "")).strip()
    try:
        await wallet_service.reject_topup(session, topup_id, admin_id=admin.id, reason=reason)
        await session.commit()
    except wallet_service.WalletError as exc:
        return _topup_back(topup_id, error=str(exc))
    return _topup_back(topup_id, saved="rejected")


@router.get("/wallet/receipts/{topup_id}")
async def wallet_receipt_file(
    topup_id: int,
    request: Request,
    admin: Admin | None = Depends(get_current_admin_optional),
    session: AsyncSession = Depends(get_session),
):
    """Serve a top-up receipt to authenticated admins only, guarding traversal."""
    lang, deny = _guard(request, admin, "view_wallet_topups")
    if deny:
        return deny
    topup = await wallet_service.get_topup(session, topup_id)
    if topup is None or not topup.receipt_path:
        return HTMLResponse(t("web.receipts.not_found", lang), status_code=404)
    resolved = payment_service.resolve_receipt_path(topup.receipt_path)
    if resolved is None:
        return HTMLResponse(t("web.receipts.not_found", lang), status_code=404)
    await audit_service.log(
        session, actor_type="admin", actor_id=admin.id, action="admin_viewed_topup_receipt",
        target_type="wallet_topup", target_id=topup.id,
    )
    filename = topup.receipt_original_name or resolved.name
    return FileResponse(
        resolved,
        media_type=topup.receipt_mime_type or "application/octet-stream",
        filename=filename, content_disposition_type="inline",
        headers={"X-Content-Type-Options": "nosniff", "Cache-Control": "private, no-store"},
    )


@router.post("/orders/{order_id}/refund")
async def order_refund(
    order_id: int,
    request: Request,
    admin: Admin | None = Depends(get_current_admin_optional),
    session: AsyncSession = Depends(get_session),
):
    lang, deny = _guard(request, admin, "refund_payments")
    if deny:
        return deny
    form = dict(await request.form())
    reason = str(form.get("reason", "")).strip()
    try:
        await wallet_service.refund_wallet_payment(
            session, order_id, admin_id=admin.id, reason=reason)
        await session.commit()
    except wallet_service.WalletError as exc:
        return _order_back(order_id, error=str(exc))
    return _order_back(order_id, saved="refunded")


# ==========================================================================
# Support tickets (Phase 9)
#
# List/detail need `view_tickets`; reply/close/assign/priority need
# `manage_tickets`. Attachments are served to authenticated `view_tickets`
# admins only, path-traversal guarded.
# ==========================================================================
_TICKET_STATUS_CLASS = {
    "open": "", "pending_admin": "warn", "pending_user": "", "closed": "ok",
}


def _ticket_back(ticket_id: int, *, saved: str = "", error: str = "") -> RedirectResponse:
    q = f"saved={quote(saved)}" if saved else f"error={quote(error)}"
    return RedirectResponse(f"/admin/tickets/{ticket_id}?{q}", status_code=303)


async def _read_upload(form: dict, field: str) -> "ticket_service.TicketAttachment | None":
    """Turn an uploaded form file into a TicketAttachment, or None if absent."""
    up = form.get(field)
    if up is None or not getattr(up, "filename", ""):
        return None
    content = await up.read()
    if not content:
        return None
    return ticket_service.TicketAttachment(
        content=content, original_name=up.filename,
        mime_type=getattr(up, "content_type", None), file_id=None,
    )


@router.get("/tickets", response_class=HTMLResponse)
async def tickets_page(
    request: Request,
    admin: Admin | None = Depends(get_current_admin_optional),
    session: AsyncSession = Depends(get_session),
    status: str = "",
    assigned: str = "",
    saved: str = "",
    error: str = "",
):
    lang, deny = _guard(request, admin, "view_tickets")
    if deny:
        return deny
    assigned_admin_id = admin.id if assigned == "me" else None
    tickets = await ticket_service.list_admin_tickets(
        session, status=(status or None), assigned_admin_id=assigned_admin_id, limit=200,
    )
    counts = await ticket_service.count_by_status(session)
    return render_template(
        "tickets.html",
        _ctx(request, admin, tickets=tickets, counts=counts, status=status,
             assigned=assigned, status_class=_TICKET_STATUS_CLASS,
             can_manage=has_permission(admin.role, "manage_tickets"),
             saved=saved, error=error),
    )


@router.get("/tickets/{ticket_id}", response_class=HTMLResponse)
async def ticket_detail_page(
    ticket_id: int,
    request: Request,
    admin: Admin | None = Depends(get_current_admin_optional),
    session: AsyncSession = Depends(get_session),
    saved: str = "",
    error: str = "",
):
    lang, deny = _guard(request, admin, "view_tickets")
    if deny:
        return deny
    ticket = await ticket_service.get_ticket(session, ticket_id)
    if ticket is None:
        return RedirectResponse("/admin/tickets", status_code=302)
    from app.models.ticket import TICKET_PRIORITIES
    return render_template(
        "ticket_detail.html",
        _ctx(request, admin, ticket=ticket, messages=ticket.messages,
             priorities=TICKET_PRIORITIES, status_class=_TICKET_STATUS_CLASS,
             attachments_enabled=await ticket_service.attachments_enabled(session),
             can_manage=has_permission(admin.role, "manage_tickets"),
             saved=saved, error=error),
    )


@router.post("/tickets/{ticket_id}/reply")
async def ticket_reply(
    ticket_id: int,
    request: Request,
    admin: Admin | None = Depends(get_current_admin_optional),
    session: AsyncSession = Depends(get_session),
):
    lang, deny = _guard(request, admin, "manage_tickets")
    if deny:
        return deny
    form = dict(await request.form())
    message = str(form.get("message", "")).strip()
    try:
        attachment = await _read_upload(form, "attachment")
        ticket = await ticket_service.add_admin_reply(
            session, ticket_id, admin.id, message, attachment=attachment)
        await session.commit()
    except ticket_service.TicketError as exc:
        return _ticket_back(ticket_id, error=str(exc))
    # Best-effort: tell the user a staff reply landed.
    await ticket_service.notify_user(
        None, ticket.user, "ticket.notify.admin_reply", number=ticket.ticket_number)
    return _ticket_back(ticket_id, saved="replied")


@router.post("/tickets/{ticket_id}/close")
async def ticket_close(
    ticket_id: int,
    request: Request,
    admin: Admin | None = Depends(get_current_admin_optional),
    session: AsyncSession = Depends(get_session),
):
    lang, deny = _guard(request, admin, "manage_tickets")
    if deny:
        return deny
    try:
        await ticket_service.close_ticket(
            session, ticket_id, actor_id=admin.id, actor_type="admin")
        await session.commit()
    except ticket_service.TicketError as exc:
        return _ticket_back(ticket_id, error=str(exc))
    return _ticket_back(ticket_id, saved="closed")


@router.post("/tickets/{ticket_id}/assign")
async def ticket_assign(
    ticket_id: int,
    request: Request,
    admin: Admin | None = Depends(get_current_admin_optional),
    session: AsyncSession = Depends(get_session),
):
    lang, deny = _guard(request, admin, "manage_tickets")
    if deny:
        return deny
    try:
        await ticket_service.assign_ticket(session, ticket_id, admin.id)
        await session.commit()
    except ticket_service.TicketError as exc:
        return _ticket_back(ticket_id, error=str(exc))
    return _ticket_back(ticket_id, saved="assigned")


@router.post("/tickets/{ticket_id}/priority")
async def ticket_priority(
    ticket_id: int,
    request: Request,
    admin: Admin | None = Depends(get_current_admin_optional),
    session: AsyncSession = Depends(get_session),
):
    lang, deny = _guard(request, admin, "manage_tickets")
    if deny:
        return deny
    form = dict(await request.form())
    priority = str(form.get("priority", "")).strip()
    try:
        await ticket_service.set_priority(session, ticket_id, priority, admin_id=admin.id)
        await session.commit()
    except ticket_service.TicketError as exc:
        return _ticket_back(ticket_id, error=str(exc))
    return _ticket_back(ticket_id, saved="priority")


@router.get("/tickets/attachments/{message_id}")
async def ticket_attachment_file(
    message_id: int,
    request: Request,
    admin: Admin | None = Depends(get_current_admin_optional),
    session: AsyncSession = Depends(get_session),
):
    """Serve a ticket attachment to authenticated admins only, guarding traversal."""
    lang, deny = _guard(request, admin, "view_tickets")
    if deny:
        return deny
    from app.models.ticket_message import TicketMessage
    msg = await session.get(TicketMessage, message_id)
    if msg is None or not msg.attachment_path:
        return HTMLResponse(t("web.tickets.attach_not_found", lang), status_code=404)
    resolved = ticket_service.resolve_attachment_path(msg.attachment_path)
    if resolved is None:
        return HTMLResponse(t("web.tickets.attach_not_found", lang), status_code=404)
    await audit_service.log(
        session, actor_type="admin", actor_id=admin.id,
        action="admin_viewed_ticket_attachment", target_type="ticket_message",
        target_id=msg.id,
    )
    return FileResponse(
        resolved,
        media_type=msg.attachment_mime_type or "application/octet-stream",
        filename=msg.attachment_original_name or resolved.name,
        content_disposition_type="inline",
        headers={"X-Content-Type-Options": "nosniff", "Cache-Control": "private, no-store"},
    )


# ==========================================================================
# Tutorials / knowledge base (Phase 9). All routes need `manage_tutorials`.
# ==========================================================================
def _tutorials_back(*, saved: str = "", error: str = "") -> RedirectResponse:
    q = f"saved={quote(saved)}" if saved else f"error={quote(error)}"
    return RedirectResponse(f"/admin/tutorials?{q}", status_code=303)


def _tutorial_form_values(form: dict) -> dict:
    return {
        "title": str(form.get("title", "")).strip(),
        "content": str(form.get("content", "")),
        "category_id": _parse_int_opt(form.get("category_id")),
        "platform": (str(form.get("platform", "")).strip() or None),
        "product_type": (str(form.get("product_type", "")).strip() or None),
        "sort_order": _parse_int_opt(form.get("sort_order")) or 0,
        "is_active": "is_active" in form,
    }


@router.get("/tutorials", response_class=HTMLResponse)
async def tutorials_page(
    request: Request,
    admin: Admin | None = Depends(get_current_admin_optional),
    session: AsyncSession = Depends(get_session),
    saved: str = "",
    error: str = "",
):
    lang, deny = _guard(request, admin, "manage_tutorials")
    if deny:
        return deny
    tutorials = await tutorial_service.list_tutorials(session)
    return render_template(
        "tutorials.html",
        _ctx(request, admin, tutorials=tutorials, saved=saved, error=error),
    )


async def _tutorial_form_ctx(session: AsyncSession) -> dict:
    from app.models.tutorial import TUTORIAL_PLATFORMS, TUTORIAL_PRODUCT_TYPES
    return {
        "categories": await tutorial_service.list_categories(session),
        "platforms": TUTORIAL_PLATFORMS,
        "product_types": TUTORIAL_PRODUCT_TYPES,
    }


@router.get("/tutorials/create", response_class=HTMLResponse)
async def tutorial_new_page(
    request: Request,
    admin: Admin | None = Depends(get_current_admin_optional),
    session: AsyncSession = Depends(get_session),
):
    lang, deny = _guard(request, admin, "manage_tutorials")
    if deny:
        return deny
    return render_template(
        "tutorial_form.html",
        _ctx(request, admin, tutorial=None, error="", **await _tutorial_form_ctx(session)),
    )


@router.post("/tutorials/create")
async def tutorial_create_submit(
    request: Request,
    admin: Admin | None = Depends(get_current_admin_optional),
    session: AsyncSession = Depends(get_session),
):
    lang, deny = _guard(request, admin, "manage_tutorials")
    if deny:
        return deny
    form = dict(await request.form())
    try:
        await tutorial_service.create_tutorial(
            session, actor_id=admin.id, **_tutorial_form_values(form))
        await session.commit()
    except tutorial_service.TutorialError as exc:
        return _tutorials_back(error=str(exc))
    return _tutorials_back(saved="created")


@router.get("/tutorials/{tutorial_id}/edit", response_class=HTMLResponse)
async def tutorial_edit_page(
    tutorial_id: int,
    request: Request,
    admin: Admin | None = Depends(get_current_admin_optional),
    session: AsyncSession = Depends(get_session),
):
    lang, deny = _guard(request, admin, "manage_tutorials")
    if deny:
        return deny
    tutorial = await tutorial_service.get_tutorial(session, tutorial_id)
    if tutorial is None:
        return RedirectResponse("/admin/tutorials", status_code=302)
    return render_template(
        "tutorial_form.html",
        _ctx(request, admin, tutorial=tutorial, error="", **await _tutorial_form_ctx(session)),
    )


@router.post("/tutorials/{tutorial_id}/edit")
async def tutorial_edit_submit(
    tutorial_id: int,
    request: Request,
    admin: Admin | None = Depends(get_current_admin_optional),
    session: AsyncSession = Depends(get_session),
):
    lang, deny = _guard(request, admin, "manage_tutorials")
    if deny:
        return deny
    form = dict(await request.form())
    values = _tutorial_form_values(form)
    # category_id 0/None from the form means "no category".
    values["category_id"] = values["category_id"] or 0
    try:
        updated = await tutorial_service.update_tutorial(
            session, tutorial_id, actor_id=admin.id, **values)
        if updated is None:
            return _tutorials_back(error="not found")
        await session.commit()
    except tutorial_service.TutorialError as exc:
        return _tutorials_back(error=str(exc))
    return _tutorials_back(saved="updated")


@router.post("/tutorials/{tutorial_id}/toggle-active")
async def tutorial_toggle(
    tutorial_id: int,
    request: Request,
    admin: Admin | None = Depends(get_current_admin_optional),
    session: AsyncSession = Depends(get_session),
):
    lang, deny = _guard(request, admin, "manage_tutorials")
    if deny:
        return deny
    await tutorial_service.toggle_active(session, tutorial_id, actor_id=admin.id)
    await session.commit()
    return _tutorials_back(saved="toggled")


@router.get("/tutorial-categories", response_class=HTMLResponse)
async def tutorial_categories_page(
    request: Request,
    admin: Admin | None = Depends(get_current_admin_optional),
    session: AsyncSession = Depends(get_session),
    saved: str = "",
    error: str = "",
):
    lang, deny = _guard(request, admin, "manage_tutorials")
    if deny:
        return deny
    categories = await tutorial_service.list_categories(session)
    return render_template(
        "tutorial_categories.html",
        _ctx(request, admin, categories=categories, saved=saved, error=error),
    )


def _categories_back(*, saved: str = "", error: str = "") -> RedirectResponse:
    q = f"saved={quote(saved)}" if saved else f"error={quote(error)}"
    return RedirectResponse(f"/admin/tutorial-categories?{q}", status_code=303)


@router.post("/tutorial-categories/create")
async def tutorial_category_create(
    request: Request,
    admin: Admin | None = Depends(get_current_admin_optional),
    session: AsyncSession = Depends(get_session),
):
    lang, deny = _guard(request, admin, "manage_tutorials")
    if deny:
        return deny
    form = dict(await request.form())
    try:
        await tutorial_service.create_category(
            session, str(form.get("title", "")),
            sort_order=_parse_int_opt(form.get("sort_order")) or 0,
            is_active="is_active" in form, actor_id=admin.id)
        await session.commit()
    except tutorial_service.TutorialError as exc:
        return _categories_back(error=str(exc))
    return _categories_back(saved="created")


@router.post("/tutorial-categories/{category_id}/edit")
async def tutorial_category_edit(
    category_id: int,
    request: Request,
    admin: Admin | None = Depends(get_current_admin_optional),
    session: AsyncSession = Depends(get_session),
):
    lang, deny = _guard(request, admin, "manage_tutorials")
    if deny:
        return deny
    form = dict(await request.form())
    try:
        updated = await tutorial_service.update_category(
            session, category_id, title=str(form.get("title", "")),
            sort_order=_parse_int_opt(form.get("sort_order")) or 0,
            is_active="is_active" in form, actor_id=admin.id)
        if updated is None:
            return _categories_back(error="not found")
        await session.commit()
    except tutorial_service.TutorialError as exc:
        return _categories_back(error=str(exc))
    return _categories_back(saved="updated")


# ==========================================================================
# Marketing: coupons (Phase 10)
#
# List/usages need `view_coupons`; create/edit/deactivate need `manage_coupons`.
# ==========================================================================
def _parse_dt_opt(raw: object):
    """Parse an <input type=datetime-local> value into a tz-aware UTC datetime."""
    from datetime import datetime, timezone
    text = str(raw or "").strip()
    if not text:
        return None
    for fmt in ("%Y-%m-%dT%H:%M", "%Y-%m-%d %H:%M", "%Y-%m-%d"):
        try:
            return datetime.strptime(text, fmt).replace(tzinfo=timezone.utc)
        except ValueError:
            continue
    return None


def _coupon_form_values(form: dict) -> dict:
    return {
        "code": str(form.get("code", "")).strip(),
        "title": (str(form.get("title", "")).strip() or None),
        "description": (str(form.get("description", "")).strip() or None),
        "discount_type": str(form.get("discount_type", "percent")).strip(),
        "discount_value": _parse_int_opt(form.get("discount_value")) or 0,
        "max_discount_amount": _parse_int_opt(form.get("max_discount_amount")),
        "min_order_amount": _parse_int_opt(form.get("min_order_amount")),
        "usage_limit": _parse_int_opt(form.get("usage_limit")),
        "usage_limit_per_user": _parse_int_opt(form.get("usage_limit_per_user")),
        "starts_at": _parse_dt_opt(form.get("starts_at")),
        "expires_at": _parse_dt_opt(form.get("expires_at")),
        "product_id": _parse_int_opt(form.get("product_id")),
        "product_type": (str(form.get("product_type", "")).strip() or None),
        "applies_to_action": (str(form.get("applies_to_action", "")).strip() or None),
        "is_active": "is_active" in form,
    }


async def _coupon_form_ctx(session: AsyncSession) -> dict:
    from app.models.coupon import COUPON_ACTIONS, COUPON_PRODUCT_TYPES
    return {
        "products": await product_service.list_for_admin(session),
        "product_types": COUPON_PRODUCT_TYPES,
        "actions": COUPON_ACTIONS,
    }


@router.get("/coupons", response_class=HTMLResponse)
async def coupons_page(
    request: Request,
    admin: Admin | None = Depends(get_current_admin_optional),
    session: AsyncSession = Depends(get_session),
    saved: str = "",
    error: str = "",
):
    lang, deny = _guard(request, admin, "view_coupons")
    if deny:
        return deny
    coupons = await coupon_service.list_coupons(session, limit=300)
    return render_template(
        "coupons.html",
        _ctx(request, admin, coupons=coupons,
             can_manage=has_permission(admin.role, "manage_coupons"),
             saved=saved, error=error),
    )


@router.get("/coupons/create", response_class=HTMLResponse)
async def coupon_new_page(
    request: Request,
    admin: Admin | None = Depends(get_current_admin_optional),
    session: AsyncSession = Depends(get_session),
):
    lang, deny = _guard(request, admin, "manage_coupons")
    if deny:
        return deny
    return render_template(
        "coupon_form.html",
        _ctx(request, admin, coupon=None, error="", **await _coupon_form_ctx(session)),
    )


def _coupons_back(*, saved: str = "", error: str = "") -> RedirectResponse:
    q = f"saved={quote(saved)}" if saved else f"error={quote(error)}"
    return RedirectResponse(f"/admin/coupons?{q}", status_code=303)


@router.post("/coupons/create")
async def coupon_create_submit(
    request: Request,
    admin: Admin | None = Depends(get_current_admin_optional),
    session: AsyncSession = Depends(get_session),
):
    lang, deny = _guard(request, admin, "manage_coupons")
    if deny:
        return deny
    form = dict(await request.form())
    try:
        await coupon_service.create_coupon(session, admin_id=admin.id, **_coupon_form_values(form))
        await session.commit()
    except coupon_service.CouponError as exc:
        return _coupons_back(error=str(exc))
    return _coupons_back(saved="created")


@router.get("/coupons/{coupon_id}/edit", response_class=HTMLResponse)
async def coupon_edit_page(
    coupon_id: int,
    request: Request,
    admin: Admin | None = Depends(get_current_admin_optional),
    session: AsyncSession = Depends(get_session),
):
    lang, deny = _guard(request, admin, "manage_coupons")
    if deny:
        return deny
    coupon = await coupon_service.get_coupon(session, coupon_id)
    if coupon is None:
        return RedirectResponse("/admin/coupons", status_code=302)
    return render_template(
        "coupon_form.html",
        _ctx(request, admin, coupon=coupon, error="", **await _coupon_form_ctx(session)),
    )


@router.post("/coupons/{coupon_id}/edit")
async def coupon_edit_submit(
    coupon_id: int,
    request: Request,
    admin: Admin | None = Depends(get_current_admin_optional),
    session: AsyncSession = Depends(get_session),
):
    lang, deny = _guard(request, admin, "manage_coupons")
    if deny:
        return deny
    form = dict(await request.form())
    try:
        updated = await coupon_service.update_coupon(
            session, coupon_id, admin_id=admin.id, **_coupon_form_values(form))
        if updated is None:
            return _coupons_back(error="not found")
        await session.commit()
    except coupon_service.CouponError as exc:
        return _coupons_back(error=str(exc))
    return _coupons_back(saved="updated")


@router.post("/coupons/{coupon_id}/deactivate")
async def coupon_deactivate(
    coupon_id: int,
    request: Request,
    admin: Admin | None = Depends(get_current_admin_optional),
    session: AsyncSession = Depends(get_session),
):
    lang, deny = _guard(request, admin, "manage_coupons")
    if deny:
        return deny
    await coupon_service.deactivate_coupon(session, coupon_id, admin_id=admin.id)
    await session.commit()
    return _coupons_back(saved="deactivated")


@router.get("/coupons/{coupon_id}/usages", response_class=HTMLResponse)
async def coupon_usages_page(
    request: Request,
    coupon_id: int,
    admin: Admin | None = Depends(get_current_admin_optional),
    session: AsyncSession = Depends(get_session),
):
    lang, deny = _guard(request, admin, "view_coupons")
    if deny:
        return deny
    coupon = await coupon_service.get_coupon(session, coupon_id)
    if coupon is None:
        return RedirectResponse("/admin/coupons", status_code=302)
    usages = await coupon_service.list_coupon_usages(session, coupon_id)
    return render_template(
        "coupon_usages.html",
        _ctx(request, admin, coupon=coupon, usages=usages),
    )


# ==========================================================================
# Marketing: referrals + rewards (Phase 10). All need `manage_referrals`.
# ==========================================================================
@router.get("/referrals", response_class=HTMLResponse)
async def referrals_page(
    request: Request,
    admin: Admin | None = Depends(get_current_admin_optional),
    session: AsyncSession = Depends(get_session),
):
    lang, deny = _guard(request, admin, "manage_referrals")
    if deny:
        return deny
    from sqlalchemy import select
    from app.models.user import User
    rows = (await session.execute(
        select(User).where(User.referrer_id.is_not(None))
        .order_by(User.referral_registered_at.desc().nulls_last(), User.id.desc()).limit(300)
    )).scalars().all()
    referrers = {u.referrer_id: None for u in rows}
    for rid in list(referrers):
        referrers[rid] = await session.get(User, rid)
    return render_template(
        "referrals.html",
        _ctx(request, admin, referred=rows, referrers=referrers),
    )


def _rewards_back(*, saved: str = "", error: str = "") -> RedirectResponse:
    q = f"saved={quote(saved)}" if saved else f"error={quote(error)}"
    return RedirectResponse(f"/admin/referral-rewards?{q}", status_code=303)


@router.get("/referral-rewards", response_class=HTMLResponse)
async def referral_rewards_page(
    request: Request,
    admin: Admin | None = Depends(get_current_admin_optional),
    session: AsyncSession = Depends(get_session),
    status: str = "",
    saved: str = "",
    error: str = "",
):
    lang, deny = _guard(request, admin, "manage_referrals")
    if deny:
        return deny
    rewards = await referral_service.list_rewards(session, status=(status or None), limit=300)
    return render_template(
        "referral_rewards.html",
        _ctx(request, admin, rewards=rewards, status=status, saved=saved, error=error),
    )


@router.post("/referral-rewards/{reward_id}/approve")
async def referral_reward_approve(
    reward_id: int,
    request: Request,
    admin: Admin | None = Depends(get_current_admin_optional),
    session: AsyncSession = Depends(get_session),
):
    lang, deny = _guard(request, admin, "manage_referrals")
    if deny:
        return deny
    try:
        await referral_service.approve_reward(session, reward_id, admin.id)
    except referral_service.ReferralError as exc:
        return _rewards_back(error=str(exc))
    return _rewards_back(saved="approved")


@router.post("/referral-rewards/{reward_id}/pay")
async def referral_reward_pay(
    reward_id: int,
    request: Request,
    admin: Admin | None = Depends(get_current_admin_optional),
    session: AsyncSession = Depends(get_session),
):
    lang, deny = _guard(request, admin, "manage_referrals")
    if deny:
        return deny
    try:
        await referral_service.pay_reward_to_wallet(session, reward_id, admin_id=admin.id)
    except referral_service.ReferralError as exc:
        return _rewards_back(error=str(exc))
    return _rewards_back(saved="paid")


@router.post("/referral-rewards/{reward_id}/reject")
async def referral_reward_reject(
    reward_id: int,
    request: Request,
    admin: Admin | None = Depends(get_current_admin_optional),
    session: AsyncSession = Depends(get_session),
):
    lang, deny = _guard(request, admin, "manage_referrals")
    if deny:
        return deny
    form = dict(await request.form())
    reason = str(form.get("reason", "")).strip() or "rejected by admin"
    try:
        await referral_service.reject_reward(session, reward_id, admin.id, reason)
        await session.commit()
    except referral_service.ReferralError as exc:
        return _rewards_back(error=str(exc))
    return _rewards_back(saved="rejected")


# ==========================================================================
# Reports & Analytics (Phase 11)
# --------------------------------------------------------------------------
# Read-only report pages, chart-data JSON endpoints, and CSV exports. Every
# route is admin-authed and gated on a report permission; each view/export
# writes an audit row (the range + report name, never the report contents).
# ==========================================================================
def _report_filter(request: Request) -> dict:
    """Read preset/start/end from the query string into a filter dict.

    With no parameters the default window is the last 30 days.
    """
    qp = request.query_params
    preset = qp.get("preset") or None
    start = qp.get("start") or None
    end = qp.get("end") or None
    if not preset and not start and not end:
        preset = "last_30_days"
    start_dt, end_dt = report_service.parse_date_range(start, end, preset)
    return {"preset": preset, "start": start, "end": end,
            "start_dt": start_dt, "end_dt": end_dt}


def _report_ctx(request: Request, admin: Admin | None, flt: dict, report_path: str, **extra):
    return _ctx(
        request, admin,
        report_path=report_path,
        report_presets=report_service.DATE_PRESETS,
        preset=flt["preset"], start=flt["start"], end=flt["end"],
        **extra,
    )


async def _audit_report(session, admin, request, name: str, action: str, flt: dict) -> None:
    bits = [f"report={name}"]
    if flt["preset"]:
        bits.append(f"preset={flt['preset']}")
    if flt["start"]:
        bits.append(f"start={flt['start']}")
    if flt["end"]:
        bits.append(f"end={flt['end']}")
    await audit_service.log(
        session, actor_type="admin", actor_id=admin.id, action=action,
        target_type="report", target_id=name, meta=" ".join(bits),
        ip_address=_client_ip(request),
    )


def _csv_response(content: str, filename: str) -> Response:
    return Response(
        content=content,
        media_type="text/csv; charset=utf-8",
        headers={"Content-Disposition": f'attachment; filename="{filename}"'},
    )


# --- Report pages ---------------------------------------------------------
@router.get("/reports", response_class=HTMLResponse)
async def reports_overview(
    request: Request,
    admin: Admin | None = Depends(get_current_admin_optional),
    session: AsyncSession = Depends(get_session),
):
    lang, deny = _guard(request, admin, "view_reports")
    if deny:
        return deny
    flt = _report_filter(request)
    summary = await report_service.get_dashboard_summary(session, flt["start_dt"], flt["end_dt"])
    await _audit_report(session, admin, request, "overview", "report.viewed", flt)
    return render_template(
        "reports_overview.html",
        _report_ctx(request, admin, flt, "/admin/reports", summary=summary),
    )


@router.get("/reports/sales", response_class=HTMLResponse)
async def reports_sales(
    request: Request,
    admin: Admin | None = Depends(get_current_admin_optional),
    session: AsyncSession = Depends(get_session),
):
    lang, deny = _guard(request, admin, "view_financial_reports")
    if deny:
        return deny
    flt = _report_filter(request)
    s, e = flt["start_dt"], flt["end_dt"]
    ctx = _report_ctx(
        request, admin, flt, "/admin/reports/sales",
        revenue=await report_service.get_revenue_summary(session, s, e),
        by_day=await report_service.get_sales_by_day(session, s, e),
        by_product=await report_service.get_sales_by_product(session, s, e),
        by_method=await report_service.get_sales_by_payment_method(session, s, e),
        by_type=await report_service.get_sales_by_product_type(session, s, e),
    )
    await _audit_report(session, admin, request, "sales", "report.financial_viewed", flt)
    return render_template("reports_sales.html", ctx)


@router.get("/reports/orders", response_class=HTMLResponse)
async def reports_orders(
    request: Request,
    admin: Admin | None = Depends(get_current_admin_optional),
    session: AsyncSession = Depends(get_session),
):
    lang, deny = _guard(request, admin, "view_reports")
    if deny:
        return deny
    flt = _report_filter(request)
    s, e = flt["start_dt"], flt["end_dt"]
    ctx = _report_ctx(
        request, admin, flt, "/admin/reports/orders",
        summary=await report_service.get_order_summary(session, s, e),
        by_action=await report_service.get_orders_by_action_type(session, s, e),
        recent=await report_service.get_recent_orders(session, limit=20),
        failed_pending=await report_service.get_failed_or_pending_orders(session, limit=20),
    )
    await _audit_report(session, admin, request, "orders", "report.viewed", flt)
    return render_template("reports_orders.html", ctx)


@router.get("/reports/payments", response_class=HTMLResponse)
async def reports_payments(
    request: Request,
    admin: Admin | None = Depends(get_current_admin_optional),
    session: AsyncSession = Depends(get_session),
):
    lang, deny = _guard(request, admin, "view_financial_reports")
    if deny:
        return deny
    flt = _report_filter(request)
    s, e = flt["start_dt"], flt["end_dt"]
    ctx = _report_ctx(
        request, admin, flt, "/admin/reports/payments",
        by_status=await report_service.get_payments_by_status(session, s, e),
        by_method=await report_service.get_payments_by_method(session, s, e),
        pending=await report_service.get_pending_receipt_summary(session),
        topups=await report_service.get_wallet_topup_summary(session, s, e),
    )
    await _audit_report(session, admin, request, "payments", "report.financial_viewed", flt)
    return render_template("reports_payments.html", ctx)


@router.get("/reports/wallet", response_class=HTMLResponse)
async def reports_wallet(
    request: Request,
    admin: Admin | None = Depends(get_current_admin_optional),
    session: AsyncSession = Depends(get_session),
):
    lang, deny = _guard(request, admin, "view_financial_reports")
    if deny:
        return deny
    flt = _report_filter(request)
    s, e = flt["start_dt"], flt["end_dt"]
    ctx = _report_ctx(
        request, admin, flt, "/admin/reports/wallet",
        by_type=await report_service.get_wallet_transaction_summary(session, s, e),
        changes=await report_service.get_wallet_balance_changes(session, s, e),
        top_users=await report_service.get_top_wallet_users(session, limit=20),
    )
    await _audit_report(session, admin, request, "wallet", "report.financial_viewed", flt)
    return render_template("reports_wallet.html", ctx)


@router.get("/reports/products", response_class=HTMLResponse)
async def reports_products(
    request: Request,
    admin: Admin | None = Depends(get_current_admin_optional),
    session: AsyncSession = Depends(get_session),
):
    lang, deny = _guard(request, admin, "view_reports")
    if deny:
        return deny
    flt = _report_filter(request)
    s, e = flt["start_dt"], flt["end_dt"]
    ctx = _report_ctx(
        request, admin, flt, "/admin/reports/products",
        summary=await report_service.get_product_summary(session, s, e),
        top_revenue=await report_service.get_top_products_by_revenue(session, s, e),
        top_orders=await report_service.get_top_products_by_orders(session, s, e),
        inactive=await report_service.get_inactive_products(session),
        low_stock=await report_service.get_low_stock_license_products(session),
    )
    await _audit_report(session, admin, request, "products", "report.viewed", flt)
    return render_template("reports_products.html", ctx)


@router.get("/reports/users", response_class=HTMLResponse)
async def reports_users(
    request: Request,
    admin: Admin | None = Depends(get_current_admin_optional),
    session: AsyncSession = Depends(get_session),
):
    lang, deny = _guard(request, admin, "view_user_reports")
    if deny:
        return deny
    flt = _report_filter(request)
    s, e = flt["start_dt"], flt["end_dt"]
    ctx = _report_ctx(
        request, admin, flt, "/admin/reports/users",
        summary=await report_service.get_user_summary(session, s, e),
        growth=await report_service.get_user_growth_by_day(session, s, e),
        blocked_restricted=await report_service.get_blocked_restricted_users(session),
    )
    await _audit_report(session, admin, request, "users", "report.user_viewed", flt)
    return render_template("reports_users.html", ctx)


@router.get("/reports/licenses", response_class=HTMLResponse)
async def reports_licenses(
    request: Request,
    admin: Admin | None = Depends(get_current_admin_optional),
    session: AsyncSession = Depends(get_session),
):
    lang, deny = _guard(request, admin, "view_service_reports")
    if deny:
        return deny
    flt = _report_filter(request)
    s, e = flt["start_dt"], flt["end_dt"]
    ctx = _report_ctx(
        request, admin, flt, "/admin/reports/licenses",
        stock=await report_service.get_license_stock_summary(session),
        sales=await report_service.get_license_sales_summary(session, s, e),
        low_stock=await report_service.get_low_stock_license_products(session),
    )
    await _audit_report(session, admin, request, "licenses", "report.service_viewed", flt)
    return render_template("reports_licenses.html", ctx)


@router.get("/reports/v2ray", response_class=HTMLResponse)
async def reports_v2ray(
    request: Request,
    admin: Admin | None = Depends(get_current_admin_optional),
    session: AsyncSession = Depends(get_session),
):
    lang, deny = _guard(request, admin, "view_service_reports")
    if deny:
        return deny
    flt = _report_filter(request)
    warn_days = await SettingsService(session).get_int("v2ray_expiry_warning_days", 3)
    ctx = _report_ctx(
        request, admin, flt, "/admin/reports/v2ray",
        summary=await report_service.get_v2ray_service_summary(session),
        expiring=await report_service.get_v2ray_expiring_soon(session, days=max(warn_days, 1)),
        problems=await report_service.get_v2ray_over_quota_or_failed(session),
        usage=await report_service.get_v2ray_usage_summary(session),
        warn_days=max(warn_days, 1),
    )
    await _audit_report(session, admin, request, "v2ray", "report.service_viewed", flt)
    return render_template("reports_v2ray.html", ctx)


@router.get("/reports/marketing", response_class=HTMLResponse)
async def reports_marketing(
    request: Request,
    admin: Admin | None = Depends(get_current_admin_optional),
    session: AsyncSession = Depends(get_session),
):
    lang, deny = _guard(request, admin, "view_financial_reports")
    if deny:
        return deny
    flt = _report_filter(request)
    s, e = flt["start_dt"], flt["end_dt"]
    ctx = _report_ctx(
        request, admin, flt, "/admin/reports/marketing",
        coupons=await report_service.get_coupon_usage_summary(session, s, e),
        referrals=await report_service.get_referral_reward_summary(session, s, e),
        coupons_available=report_service.COUPONS_AVAILABLE,
        referrals_available=report_service.REFERRALS_AVAILABLE,
    )
    await _audit_report(session, admin, request, "marketing", "report.financial_viewed", flt)
    return render_template("reports_marketing.html", ctx)


@router.get("/reports/support", response_class=HTMLResponse)
async def reports_support(
    request: Request,
    admin: Admin | None = Depends(get_current_admin_optional),
    session: AsyncSession = Depends(get_session),
):
    lang, deny = _guard(request, admin, "view_reports")
    if deny:
        return deny
    flt = _report_filter(request)
    s, e = flt["start_dt"], flt["end_dt"]
    ctx = _report_ctx(
        request, admin, flt, "/admin/reports/support",
        tickets=await report_service.get_ticket_summary(session, s, e),
        open_tickets=await report_service.get_open_ticket_summary(session),
        tickets_available=report_service.TICKETS_AVAILABLE,
    )
    await _audit_report(session, admin, request, "support", "report.viewed", flt)
    return render_template("reports_support.html", ctx)


@router.get("/reports/exports", response_class=HTMLResponse)
async def reports_exports(
    request: Request,
    admin: Admin | None = Depends(get_current_admin_optional),
    session: AsyncSession = Depends(get_session),
):
    lang, deny = _guard(request, admin, "export_reports")
    if deny:
        return deny
    flt = _report_filter(request)
    ctx = _report_ctx(
        request, admin, flt, "/admin/reports/exports",
        coupons_available=report_service.COUPONS_AVAILABLE,
        referrals_available=report_service.REFERRALS_AVAILABLE,
        tickets_available=report_service.TICKETS_AVAILABLE,
    )
    return render_template("reports_exports.html", ctx)


# --- Chart-data JSON endpoints -------------------------------------------
@router.get("/reports/api/sales-by-day")
async def reports_api_sales_by_day(
    request: Request,
    admin: Admin | None = Depends(get_current_admin_optional),
    session: AsyncSession = Depends(get_session),
):
    lang, deny = _guard(request, admin, "view_financial_reports")
    if deny:
        return deny
    flt = _report_filter(request)
    data = await report_service.get_sales_by_day(session, flt["start_dt"], flt["end_dt"])
    return JSONResponse(data)


@router.get("/reports/api/user-growth")
async def reports_api_user_growth(
    request: Request,
    admin: Admin | None = Depends(get_current_admin_optional),
    session: AsyncSession = Depends(get_session),
):
    lang, deny = _guard(request, admin, "view_user_reports")
    if deny:
        return deny
    flt = _report_filter(request)
    data = await report_service.get_user_growth_by_day(session, flt["start_dt"], flt["end_dt"])
    return JSONResponse(data)


@router.get("/reports/api/orders-by-status")
async def reports_api_orders_by_status(
    request: Request,
    admin: Admin | None = Depends(get_current_admin_optional),
    session: AsyncSession = Depends(get_session),
):
    lang, deny = _guard(request, admin, "view_reports")
    if deny:
        return deny
    flt = _report_filter(request)
    data = await report_service.get_orders_by_status(session, flt["start_dt"], flt["end_dt"])
    return JSONResponse(data)


@router.get("/reports/api/payments-by-method")
async def reports_api_payments_by_method(
    request: Request,
    admin: Admin | None = Depends(get_current_admin_optional),
    session: AsyncSession = Depends(get_session),
):
    lang, deny = _guard(request, admin, "view_financial_reports")
    if deny:
        return deny
    flt = _report_filter(request)
    data = await report_service.get_payments_by_method(session, flt["start_dt"], flt["end_dt"])
    return JSONResponse(data)


@router.get("/reports/api/top-products")
async def reports_api_top_products(
    request: Request,
    admin: Admin | None = Depends(get_current_admin_optional),
    session: AsyncSession = Depends(get_session),
):
    lang, deny = _guard(request, admin, "view_reports")
    if deny:
        return deny
    flt = _report_filter(request)
    data = await report_service.get_top_products_by_revenue(session, flt["start_dt"], flt["end_dt"])
    return JSONResponse(data)


@router.get("/reports/api/v2ray-usage")
async def reports_api_v2ray_usage(
    request: Request,
    admin: Admin | None = Depends(get_current_admin_optional),
    session: AsyncSession = Depends(get_session),
):
    lang, deny = _guard(request, admin, "view_service_reports")
    if deny:
        return deny
    data = await report_service.get_v2ray_usage_summary(session)
    return JSONResponse(data)


# --- CSV exports ----------------------------------------------------------
@router.get("/reports/export/orders.csv")
async def export_orders_csv(
    request: Request,
    admin: Admin | None = Depends(get_current_admin_optional),
    session: AsyncSession = Depends(get_session),
):
    lang, deny = _guard(request, admin, "export_reports")
    if deny:
        return deny
    flt = _report_filter(request)
    status = request.query_params.get("status") or None
    content = await export_service.export_orders_csv(session, flt["start_dt"], flt["end_dt"], status)
    await _audit_report(session, admin, request, "orders", "report.export_created", flt)
    return _csv_response(content, export_service.export_filename("orders"))


@router.get("/reports/export/payments.csv")
async def export_payments_csv(
    request: Request,
    admin: Admin | None = Depends(get_current_admin_optional),
    session: AsyncSession = Depends(get_session),
):
    lang, deny = _guard(request, admin, "export_reports")
    if deny:
        return deny
    flt = _report_filter(request)
    method = request.query_params.get("method") or None
    status = request.query_params.get("status") or None
    content = await export_service.export_payments_csv(
        session, flt["start_dt"], flt["end_dt"], method, status)
    await _audit_report(session, admin, request, "payments", "report.export_created", flt)
    return _csv_response(content, export_service.export_filename("payments"))


@router.get("/reports/export/wallet-transactions.csv")
async def export_wallet_csv(
    request: Request,
    admin: Admin | None = Depends(get_current_admin_optional),
    session: AsyncSession = Depends(get_session),
):
    lang, deny = _guard(request, admin, "export_reports")
    if deny:
        return deny
    flt = _report_filter(request)
    content = await export_service.export_wallet_transactions_csv(
        session, flt["start_dt"], flt["end_dt"])
    await _audit_report(session, admin, request, "wallet-transactions", "report.export_created", flt)
    return _csv_response(content, export_service.export_filename("wallet-transactions"))


@router.get("/reports/export/users.csv")
async def export_users_csv(
    request: Request,
    admin: Admin | None = Depends(get_current_admin_optional),
    session: AsyncSession = Depends(get_session),
):
    lang, deny = _guard(request, admin, "export_reports")
    if deny:
        return deny
    flt = _report_filter(request)
    content = await export_service.export_users_csv(session, flt["start_dt"], flt["end_dt"])
    await _audit_report(session, admin, request, "users", "report.export_created", flt)
    return _csv_response(content, export_service.export_filename("users"))


@router.get("/reports/export/products.csv")
async def export_products_csv(
    request: Request,
    admin: Admin | None = Depends(get_current_admin_optional),
    session: AsyncSession = Depends(get_session),
):
    lang, deny = _guard(request, admin, "export_reports")
    if deny:
        return deny
    flt = _report_filter(request)
    content = await export_service.export_products_csv(session)
    await _audit_report(session, admin, request, "products", "report.export_created", flt)
    return _csv_response(content, export_service.export_filename("products"))


@router.get("/reports/export/licenses.csv")
async def export_licenses_csv(
    request: Request,
    admin: Admin | None = Depends(get_current_admin_optional),
    session: AsyncSession = Depends(get_session),
):
    lang, deny = _guard(request, admin, "export_reports")
    if deny:
        return deny
    flt = _report_filter(request)
    status = request.query_params.get("status") or None
    content = await export_service.export_licenses_csv(session, status)
    await _audit_report(session, admin, request, "licenses", "report.export_created", flt)
    return _csv_response(content, export_service.export_filename("licenses"))


@router.get("/reports/export/v2ray-services.csv")
async def export_v2ray_csv(
    request: Request,
    admin: Admin | None = Depends(get_current_admin_optional),
    session: AsyncSession = Depends(get_session),
):
    lang, deny = _guard(request, admin, "export_reports")
    if deny:
        return deny
    flt = _report_filter(request)
    status = request.query_params.get("status") or None
    content = await export_service.export_v2ray_services_csv(session, status)
    await _audit_report(session, admin, request, "v2ray-services", "report.export_created", flt)
    return _csv_response(content, export_service.export_filename("v2ray-services"))


@router.get("/reports/export/coupon-usages.csv")
async def export_coupon_usages_csv(
    request: Request,
    admin: Admin | None = Depends(get_current_admin_optional),
    session: AsyncSession = Depends(get_session),
):
    lang, deny = _guard(request, admin, "export_reports")
    if deny:
        return deny
    flt = _report_filter(request)
    content = await export_service.export_coupon_usages_csv(session, flt["start_dt"], flt["end_dt"])
    await _audit_report(session, admin, request, "coupon-usages", "report.export_created", flt)
    return _csv_response(content, export_service.export_filename("coupon-usages"))


@router.get("/reports/export/referral-rewards.csv")
async def export_referral_rewards_csv(
    request: Request,
    admin: Admin | None = Depends(get_current_admin_optional),
    session: AsyncSession = Depends(get_session),
):
    lang, deny = _guard(request, admin, "export_reports")
    if deny:
        return deny
    flt = _report_filter(request)
    content = await export_service.export_referral_rewards_csv(session, flt["start_dt"], flt["end_dt"])
    await _audit_report(session, admin, request, "referral-rewards", "report.export_created", flt)
    return _csv_response(content, export_service.export_filename("referral-rewards"))


@router.get("/reports/export/tickets.csv")
async def export_tickets_csv(
    request: Request,
    admin: Admin | None = Depends(get_current_admin_optional),
    session: AsyncSession = Depends(get_session),
):
    lang, deny = _guard(request, admin, "export_reports")
    if deny:
        return deny
    flt = _report_filter(request)
    content = await export_service.export_tickets_csv(session, flt["start_dt"], flt["end_dt"])
    await _audit_report(session, admin, request, "tickets", "report.export_created", flt)
    return _csv_response(content, export_service.export_filename("tickets"))


# ==========================================================================
# Maintenance: backups, restore, health, system info (Phase 12)
# --------------------------------------------------------------------------
# Backups hold sensitive data: downloads are permission-gated + no-store +
# path-traversal-safe (paths are resolved under storage/backups only), restore
# is owner-only and confirmation-gated, and every action is audited (metadata
# only — never backup contents or secrets).
# ==========================================================================
def _maint_ctx(request: Request, admin: Admin | None, **extra):
    return _ctx(request, admin, **extra)


async def _audit_maint(session, admin, request, action: str, *, target_id=None, meta=None) -> None:
    await audit_service.log(
        session, actor_type="admin", actor_id=admin.id, action=action,
        target_type="backup" if "backup" in action or "restore" in action else "maintenance",
        target_id=target_id, meta=meta, ip_address=_client_ip(request),
    )


def _maint_back(saved: str = "", error: str = "", path: str = "/admin/maintenance/backups"):
    q = []
    if saved:
        q.append(f"saved={quote(saved)}")
    if error:
        q.append(f"error={quote(error)}")
    sep = "?" if q else ""
    return RedirectResponse(f"{path}{sep}{'&'.join(q)}", status_code=303)


# --- Overview -------------------------------------------------------------
@router.get("/maintenance", response_class=HTMLResponse)
async def maintenance_overview(
    request: Request,
    admin: Admin | None = Depends(get_current_admin_optional),
    session: AsyncSession = Depends(get_session),
):
    lang, deny = _guard(request, admin, "view_maintenance")
    if deny:
        return deny
    svc = SettingsService(session)
    recent = await backup_service.list_backups(session, limit=5)
    completed = await backup_service.list_backups(session, status="completed", limit=1)
    ctx = _maint_ctx(
        request, admin,
        recent=recent,
        latest=(completed[0] if completed else None),
        maintenance=await svc.get_bool("maintenance_mode", False),
        maintenance_message=await svc.get_str("maintenance_message", ""),
        backups_enabled=await svc.get_bool("backups_enabled", True),
        scheduled_enabled=await svc.get_bool("scheduled_backups_enabled", False),
        can_backups=has_permission(admin.role, "manage_backups"),
        can_restore=has_permission(admin.role, "restore_backups"),
        can_health=has_permission(admin.role, "view_health"),
        saved=request.query_params.get("saved", ""),
    )
    return render_template("maintenance_overview.html", ctx)


# --- Backups list + create ------------------------------------------------
@router.get("/maintenance/backups", response_class=HTMLResponse)
async def maintenance_backups(
    request: Request,
    admin: Admin | None = Depends(get_current_admin_optional),
    session: AsyncSession = Depends(get_session),
):
    lang, deny = _guard(request, admin, "manage_backups")
    if deny:
        return deny
    svc = SettingsService(session)
    ctx = _maint_ctx(
        request, admin,
        backups=await backup_service.list_backups(session, limit=100),
        backups_enabled=await svc.get_bool("backups_enabled", True),
        download_enabled=await svc.get_bool("backup_download_enabled", True),
        can_download=has_permission(admin.role, "download_backups"),
        saved=request.query_params.get("saved", ""),
        error=request.query_params.get("error", ""),
    )
    return render_template("backups_list.html", ctx)


@router.post("/maintenance/backups/create")
async def maintenance_backup_create(
    request: Request,
    admin: Admin | None = Depends(get_current_admin_optional),
    session: AsyncSession = Depends(get_session),
    backup_type: str = Form(...),
):
    lang, deny = _guard(request, admin, "manage_backups")
    if deny:
        return deny
    if not await SettingsService(session).get_bool("backups_enabled", True):
        return _maint_back(error="disabled")
    if backup_type not in backup_service.BACKUP_TYPES:
        return _maint_back(error="type")
    job = await backup_service.create_backup_job(session, backup_type, admin_id=admin.id)
    await _audit_maint(session, admin, request, "backup_job_created", target_id=job.id, meta=f"type={backup_type}")
    await _audit_maint(session, admin, request, "backup_started", target_id=job.id)
    job = await backup_service.run_backup_job(session, job.id)
    if job and job.status == "completed":
        await _audit_maint(session, admin, request, "backup_completed", target_id=job.id,
                           meta=f"size={job.file_size}")
        return _maint_back(saved="created")
    await _audit_maint(session, admin, request, "backup_failed", target_id=(job.id if job else None),
                       meta=(job.error_message if job else "unknown"))
    return _maint_back(error="failed")


@router.get("/maintenance/backups/{backup_id}", response_class=HTMLResponse)
async def maintenance_backup_detail(
    backup_id: int,
    request: Request,
    admin: Admin | None = Depends(get_current_admin_optional),
    session: AsyncSession = Depends(get_session),
):
    lang, deny = _guard(request, admin, "manage_backups")
    if deny:
        return deny
    job = await backup_service.get_backup(session, backup_id)
    if job is None:
        return RedirectResponse("/admin/maintenance/backups?error=notfound", status_code=303)
    ctx = _maint_ctx(
        request, admin, job=job,
        can_download=has_permission(admin.role, "download_backups"),
        download_enabled=await SettingsService(session).get_bool("backup_download_enabled", True),
        verify=request.query_params.get("verify", ""),
    )
    return render_template("backup_detail.html", ctx)


@router.get("/maintenance/backups/{backup_id}/download")
async def maintenance_backup_download(
    backup_id: int,
    request: Request,
    admin: Admin | None = Depends(get_current_admin_optional),
    session: AsyncSession = Depends(get_session),
):
    lang, deny = _guard(request, admin, "download_backups")
    if deny:
        return deny
    if not await SettingsService(session).get_bool("backup_download_enabled", True):
        return _forbidden(lang)
    job = await backup_service.get_backup(session, backup_id)
    if job is None or job.status == "deleted":
        return Response(status_code=404)
    path = backup_service._abs_path(job)
    if path is None or not path.exists():
        return Response(status_code=404)
    await _audit_maint(session, admin, request, "backup_downloaded", target_id=job.id,
                       meta=f"file={job.file_name}")
    return FileResponse(
        path, filename=job.file_name or path.name, media_type="application/octet-stream",
        headers={"X-Content-Type-Options": "nosniff", "Cache-Control": "private, no-store"},
    )


@router.post("/maintenance/backups/{backup_id}/verify")
async def maintenance_backup_verify(
    backup_id: int,
    request: Request,
    admin: Admin | None = Depends(get_current_admin_optional),
    session: AsyncSession = Depends(get_session),
):
    lang, deny = _guard(request, admin, "manage_backups")
    if deny:
        return deny
    result = await backup_service.verify_backup(session, backup_id)
    await _audit_maint(session, admin, request, "backup_verified", target_id=backup_id,
                       meta=result.get("reason"))
    return RedirectResponse(
        f"/admin/maintenance/backups/{backup_id}?verify={quote(result.get('reason', ''))}",
        status_code=303,
    )


@router.post("/maintenance/backups/{backup_id}/delete")
async def maintenance_backup_delete(
    backup_id: int,
    request: Request,
    admin: Admin | None = Depends(get_current_admin_optional),
    session: AsyncSession = Depends(get_session),
):
    lang, deny = _guard(request, admin, "manage_backups")
    if deny:
        return deny
    await backup_service.delete_backup(session, backup_id, admin_id=admin.id)
    await _audit_maint(session, admin, request, "backup_deleted", target_id=backup_id)
    return _maint_back(saved="deleted")


# --- Restore (owner only) -------------------------------------------------
@router.get("/maintenance/restore", response_class=HTMLResponse)
async def maintenance_restore(
    request: Request,
    admin: Admin | None = Depends(get_current_admin_optional),
    session: AsyncSession = Depends(get_session),
):
    lang, deny = _guard(request, admin, "restore_backups")
    if deny:
        return deny
    ctx = _maint_ctx(
        request, admin,
        backups=await backup_service.list_backups(session, status="completed", limit=100),
        error=request.query_params.get("error", ""),
    )
    return render_template("restore.html", ctx)


@router.post("/maintenance/restore/plan", response_class=HTMLResponse)
async def maintenance_restore_plan(
    request: Request,
    admin: Admin | None = Depends(get_current_admin_optional),
    session: AsyncSession = Depends(get_session),
    backup_id: int = Form(...),
):
    lang, deny = _guard(request, admin, "restore_backups")
    if deny:
        return deny
    plan = await restore_service.create_restore_plan(session, backup_id)
    if not plan.get("backup"):
        return RedirectResponse("/admin/maintenance/restore?error=notfound", status_code=303)
    token = restore_service.generate_restore_confirm_token(admin.id, backup_id)
    await _audit_maint(session, admin, request, "restore_plan_created", target_id=backup_id)
    ctx = _maint_ctx(request, admin, plan=plan, backup_id=backup_id, confirm_token=token,
                     sentinel=restore_service.RESTORE_SENTINEL)
    return render_template("restore_plan.html", ctx)


@router.post("/maintenance/restore/confirm", response_class=HTMLResponse)
async def maintenance_restore_confirm(
    request: Request,
    admin: Admin | None = Depends(get_current_admin_optional),
    session: AsyncSession = Depends(get_session),
    backup_id: int = Form(...),
    confirm_token: str = Form(""),
    confirm_phrase: str = Form(""),
):
    lang, deny = _guard(request, admin, "restore_backups")
    if deny:
        return deny
    if confirm_phrase != restore_service.RESTORE_SENTINEL:
        return RedirectResponse("/admin/maintenance/restore?error=phrase", status_code=303)
    job = await backup_service.get_backup(session, backup_id)
    if job is None:
        return RedirectResponse("/admin/maintenance/restore?error=notfound", status_code=303)
    await _audit_maint(session, admin, request, "restore_confirmed", target_id=backup_id)
    await _audit_maint(session, admin, request, "restore_started", target_id=backup_id)
    try:
        if job.backup_type == "storage":
            result = await restore_service.restore_storage_from_backup(
                session, backup_id, confirm_token, admin_id=admin.id)
        elif job.backup_type == "database":
            result = await restore_service.restore_database_from_backup(
                session, backup_id, confirm_token, admin_id=admin.id)
        else:
            result = await restore_service.restore_full_backup(
                session, backup_id, confirm_token, admin_id=admin.id)
    except restore_service.RestoreError as exc:
        await _audit_maint(session, admin, request, "restore_failed", target_id=backup_id,
                           meta=str(exc))
        return render_template("restore_result.html", _maint_ctx(
            request, admin, backup=job, result={"status": "failed", "message": str(exc)}))
    await _audit_maint(session, admin, request, "restore_completed", target_id=backup_id,
                       meta=result.get("status"))
    return render_template("restore_result.html",
                           _maint_ctx(request, admin, backup=job, result=result))


# --- Maintenance mode toggle ---------------------------------------------
@router.post("/maintenance/mode")
async def maintenance_mode_toggle(
    request: Request,
    admin: Admin | None = Depends(get_current_admin_optional),
    session: AsyncSession = Depends(get_session),
    action: str = Form(...),
    message: str = Form(""),
):
    lang, deny = _guard(request, admin, "manage_settings")
    if deny:
        return deny
    svc = SettingsService(session)
    if action == "enable":
        await svc.set("maintenance_mode", "true", actor_type="admin", actor_id=admin.id, audit=False)
        await svc.set("maintenance_message", message.strip(), actor_type="admin",
                      actor_id=admin.id, audit=False)
        await session.commit()
        await _audit_maint(session, admin, request, "maintenance_mode_enabled")
    elif action == "disable":
        await svc.set("maintenance_mode", "false", actor_type="admin", actor_id=admin.id, audit=False)
        await session.commit()
        await _audit_maint(session, admin, request, "maintenance_mode_disabled")
    return RedirectResponse("/admin/maintenance?saved=mode", status_code=303)


# --- Health / diagnostics -------------------------------------------------
@router.get("/maintenance/health", response_class=HTMLResponse)
async def maintenance_health(
    request: Request,
    admin: Admin | None = Depends(get_current_admin_optional),
    session: AsyncSession = Depends(get_session),
):
    lang, deny = _guard(request, admin, "view_health")
    if deny:
        return deny
    from app.core.redis import redis_ok
    # DB is obviously reachable (we're mid-request on it); confirm with a ping.
    try:
        await session.execute(select(func.count()).select_from(BackupJob))
        db_ok = True
    except Exception:  # noqa: BLE001
        db_ok = False
    try:
        redis_up = await redis_ok()
    except Exception:  # noqa: BLE001
        redis_up = False

    total, used, free = shutil.disk_usage(backup_service.REPO_ROOT)
    backups = await backup_service.list_backups(session, limit=500)
    backup_bytes = sum(int(b.file_size or 0) for b in backups if b.status == "completed")
    failed_jobs = [b for b in backups if b.status == "failed"][:10]

    start, end = report_service.parse_date_range(preset="last_30_days")
    summary = await report_service.get_dashboard_summary(session, start, end)

    ctx = _maint_ctx(
        request, admin,
        db_ok=db_ok, redis_up=redis_up,
        disk={"total": total, "used": used, "free": free,
              "pct": round(used / total * 100, 1) if total else 0},
        backup_bytes=backup_bytes,
        backup_count=sum(1 for b in backups if b.status == "completed"),
        failed_jobs=failed_jobs,
        attention=summary.get("attention", {}),
        version=__version__,
    )
    await _audit_maint(session, admin, request, "health_check_viewed")
    return render_template("maintenance_health.html", ctx)


# --- System info ----------------------------------------------------------
@router.get("/maintenance/system-info", response_class=HTMLResponse)
async def maintenance_system_info(
    request: Request,
    admin: Admin | None = Depends(get_current_admin_optional),
    session: AsyncSession = Depends(get_session),
):
    import platform
    import sys
    lang, deny = _guard(request, admin, "view_maintenance")
    if deny:
        return deny
    from app.config import settings as _s
    info = {
        "version": __version__,
        "app_env": _s.APP_ENV,
        "python": sys.version.split()[0],
        "platform": platform.platform(),
        # DB backend name only — NEVER the URL (it contains credentials).
        "db_backend": (session.bind.dialect.name if session.bind else "unknown"),
        "storage_root": str(backup_service.STORAGE_ROOT),
    }
    ctx = _maint_ctx(request, admin, info=info)
    return render_template("system_info.html", ctx)

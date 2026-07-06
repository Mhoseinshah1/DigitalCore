"""Server-rendered admin panel (Persian RTL) served under /admin.

Everything the admin sees lives under the /admin prefix; `/` and `/login`
redirect there for convenience. The viewer's language comes from the dc_lang
cookie (default fa) and is exposed to templates as `lang`, `rtl`, and the `_`
translator. Auth is a JWT session cookie (see app/web/deps.py).
"""
from __future__ import annotations

from pathlib import Path
from urllib.parse import quote

from fastapi import APIRouter, Depends, Form, Request
from fastapi.responses import FileResponse, HTMLResponse, JSONResponse, RedirectResponse
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
    license_service,
    order_service,
    payment_service,
    product_service,
    user_service,
    v2ray_service,
    wallet_service,
    xui_server_service,
)
from app.web.api.auth import authenticate_admin
from app.web.deps import COOKIE_NAME, get_current_admin_optional, set_session_cookie

BASE_DIR = Path(__file__).resolve().parent
templates = Jinja2Templates(directory=BASE_DIR / "templates")

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
        {"label_key": "nav.future.tickets", "icon": "🎫", "href": "/admin/tickets",
         "permission": "view_dashboard", "placeholder": True},
        {"label_key": "nav.future.coupons", "icon": "🏷", "href": "/admin/coupons",
         "permission": "manage_products", "placeholder": True},
        {"label_key": "nav.future.referrals", "icon": "🔗", "href": "/admin/referrals",
         "permission": "view_dashboard", "placeholder": True},
        {"label_key": "nav.future.backups", "icon": "💾", "href": "/admin/backups",
         "permission": "manage_settings", "placeholder": True},
        {"label_key": "nav.future.reports", "icon": "📊", "href": "/admin/reports",
         "permission": "view_dashboard", "placeholder": True},
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
    ("/tickets", "nav.future.tickets", "view_dashboard"),
    ("/coupons", "nav.future.coupons", "manage_products"),
    ("/referrals", "nav.future.referrals", "view_dashboard"),
    ("/backups", "nav.future.backups", "manage_settings"),
    ("/reports", "nav.future.reports", "view_dashboard"),
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
        return templates.TemplateResponse(
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
    return templates.TemplateResponse("login.html", _ctx(request, None, error=None))


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
        return templates.TemplateResponse(
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
    return templates.TemplateResponse(
        "dashboard.html",
        _ctx(
            request, admin,
            stats=stats,
            active_products=int(active_products),
            site_name=site_name,
            maintenance=maintenance,
            sales=sales,
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
    return templates.TemplateResponse(
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
    return templates.TemplateResponse(
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
    return templates.TemplateResponse(
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
    return templates.TemplateResponse(
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
    return templates.TemplateResponse(
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
    if type_ != "v2ray":
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
    return templates.TemplateResponse(
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
    return templates.TemplateResponse(
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
    return templates.TemplateResponse(
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
    return templates.TemplateResponse(
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
    return templates.TemplateResponse(
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
    return templates.TemplateResponse(
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
    return templates.TemplateResponse(
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
    return templates.TemplateResponse(
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
    return templates.TemplateResponse(
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
    return templates.TemplateResponse(
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
    return templates.TemplateResponse(
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
    return templates.TemplateResponse(
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
    return templates.TemplateResponse(
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
    return templates.TemplateResponse(
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
    return templates.TemplateResponse(
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
    return templates.TemplateResponse(
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
    return templates.TemplateResponse(
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
    return templates.TemplateResponse(
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
    return templates.TemplateResponse(
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
    return templates.TemplateResponse(
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
    return templates.TemplateResponse(
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
    return templates.TemplateResponse(
        "v2ray_service_detail.html",
        _ctx(request, admin, svc=svc, masked_uuid=_mask_uuid(svc.client_uuid),
             used_disp=_gb_disp(svc.used_gb), total_disp=_gb_disp(svc.total_gb),
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
    try:
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
    return templates.TemplateResponse(
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
    return templates.TemplateResponse(
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
    return templates.TemplateResponse(
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
    return templates.TemplateResponse(
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

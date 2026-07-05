"""Server-rendered HTML pages for the admin panel (bilingual fa/en).

The panel is server-rendered so the whole stack works with no separate frontend
build. The viewer's language comes from the dc_lang cookie (default fa) and is
exposed to templates as `lang`, `rtl`, and the `_` translator callable.
"""
from __future__ import annotations

from pathlib import Path
from urllib.parse import quote

from fastapi import APIRouter, Depends, Form, Request
from fastapi.responses import HTMLResponse, RedirectResponse
from fastapi.templating import Jinja2Templates
from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

from app import __version__
from app.core.defaults import (
    CATEGORIES,
    DEFAULTS,
    DEFAULTS_BY_KEY,
    category_title_for,
    description_for,
    label_for,
)
from app.core.permissions import has_permission
from app.core.security import create_access_token
from app.core.settings_service import SettingsService, coerce_out
from app.database import get_session
from app.i18n import SUPPORTED, is_rtl, normalize_lang, t
from app.models.admin import Admin
from app.models.product import PRODUCT_TYPES
from app.models.xui_inbound import XuiInbound
from app.schemas.product import ProductCreate, ProductUpdate
from app.services import product_service, xui_service
from app.xui.exceptions import XuiError
from app.xui.registry import SUPPORTED_VERSIONS
from app.web.api.auth import authenticate_admin
from app.web.deps import COOKIE_NAME, get_current_admin_optional, set_session_cookie

BASE_DIR = Path(__file__).resolve().parent
templates = Jinja2Templates(directory=BASE_DIR / "templates")

router = APIRouter(include_in_schema=False)

LANG_COOKIE = "dc_lang"

# Grouped, nested navigation. Each top-level entry is either a direct link
# (has "href") or an expandable group (has "items"). Every entry carries the
# permission needed to see it; the settings group's sub-items are generated from
# the settings CATEGORIES at render time. `placeholder=True` marks sections whose
# backend isn't built yet — they route to a "coming soon" page, never fake data.
NAV_TREE: list[dict] = [
    {"label_key": "web.nav.dashboard", "icon": "🏠", "href": "/",
     "permission": "view_dashboard"},
    {"label_key": "web.nav.sales", "icon": "🛒",
     "items": [
         {"label_key": "web.nav.products", "icon": "📦", "href": "/products",
          "permission": "manage_products"},
         {"label_key": "web.nav.orders", "icon": "🧾", "href": "/orders",
          "permission": "approve_payments", "placeholder": True},
         {"label_key": "web.nav.payments", "icon": "💳", "href": "/payments",
          "permission": "approve_payments", "placeholder": True},
     ]},
    {"label_key": "web.nav.licenses", "icon": "🔑", "href": "/licenses",
     "permission": "manage_products", "placeholder": True},
    {"label_key": "web.nav.services", "icon": "🌐", "href": "/services",
     "permission": "manage_xui", "placeholder": True},
    {"label_key": "web.nav.xui", "icon": "🖥",
     "items": [
         {"label_key": "web.nav.servers", "icon": "🖥", "href": "/servers",
          "permission": "manage_xui"},
         {"label_key": "web.nav.inbounds", "icon": "🔌", "href": "/inbounds",
          "permission": "manage_xui", "placeholder": True},
     ]},
    {"label_key": "web.nav.users", "icon": "👥", "href": "/users",
     "permission": "manage_users", "placeholder": True},
    {"label_key": "web.nav.settings", "icon": "⚙️", "permission": "manage_settings",
     "settings_group": True},
    {"label_key": "web.nav.reports_group", "icon": "📊",
     "items": [
         {"label_key": "web.nav.reports", "icon": "📊", "href": "/reports",
          "permission": "view_dashboard", "placeholder": True},
         {"label_key": "web.nav.audit_logs", "icon": "📜", "href": "/audit-logs",
          "permission": "view_audit_log", "placeholder": True},
     ]},
]

# Placeholder pages: (path, i18n title key, permission). Real routes are defined
# explicitly elsewhere; these are the not-yet-built sections referenced by NAV_TREE.
PLACEHOLDER_PAGES: list[tuple[str, str, str]] = [
    ("/orders", "web.nav.orders", "approve_payments"),
    ("/payments", "web.nav.payments", "approve_payments"),
    ("/licenses", "web.nav.licenses", "manage_products"),
    ("/services", "web.nav.services", "manage_xui"),
    ("/inbounds", "web.nav.inbounds", "manage_xui"),
    ("/users", "web.nav.users", "manage_users"),
    ("/reports", "web.nav.reports", "view_dashboard"),
    ("/audit-logs", "web.nav.audit_logs", "view_audit_log"),
]


def _resolve_lang(request: Request) -> str:
    return normalize_lang(request.cookies.get(LANG_COOKIE))


def _can(role: object, permission: str | None) -> bool:
    return permission is None or has_permission(role, permission)


def _item_path(href: str) -> str:
    """The routeable path of an href, dropping any #fragment or ?query."""
    return href.split("#", 1)[0].split("?", 1)[0]


def build_nav(role: object, lang: str, current_path: str) -> list[dict]:
    """Build the visible, RBAC-filtered navigation tree for the current viewer.

    Returns render-ready sections (labels resolved, active flags set). Groups with
    no visible sub-items are omitted entirely, as are entries the role can't access.
    """
    sections: list[dict] = []
    for node in NAV_TREE:
        if node.get("settings_group"):
            if not _can(role, node.get("permission")):
                continue
            items = [
                {
                    "label": category_title_for(cat, lang),
                    "href": f"/settings#{cat}",
                    "icon": str(meta.get("icon", "•")),
                    "active": False,  # server can't see the #fragment
                    "placeholder": False,
                }
                for cat, meta in sorted(CATEGORIES.items(), key=lambda kv: kv[1]["order"])
            ]
            sections.append({
                "label": t(node["label_key"], lang),
                "icon": node["icon"],
                "href": None,
                "active": current_path == "/settings",
                "children": items,
            })
            continue

        if "items" in node:
            items = []
            for it in node["items"]:
                if not _can(role, it.get("permission")):
                    continue
                items.append({
                    "label": t(it["label_key"], lang),
                    "href": it["href"],
                    "icon": it["icon"],
                    "active": current_path == _item_path(it["href"]),
                    "placeholder": bool(it.get("placeholder")),
                })
            if not items:
                continue
            sections.append({
                "label": t(node["label_key"], lang),
                "icon": node["icon"],
                "href": None,
                "active": any(i["active"] for i in items),
                "children": items,
            })
            continue

        # Direct link.
        if not _can(role, node.get("permission")):
            continue
        sections.append({
            "label": t(node["label_key"], lang),
            "icon": node["icon"],
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


def _make_placeholder(title_key: str, permission: str):
    """Build a thin, auth+RBAC-gated handler that renders the 'coming soon' shell."""

    async def handler(
        request: Request,
        admin: Admin | None = Depends(get_current_admin_optional),
    ):
        if admin is None:
            return RedirectResponse("/login", status_code=302)
        lang = _resolve_lang(request)
        if not has_permission(admin.role, permission):
            return _forbidden(lang)
        return templates.TemplateResponse(
            "placeholder.html", _ctx(request, admin, page_title_key=title_key)
        )

    return handler


# Register the not-yet-built sections referenced by NAV_TREE as placeholder pages.
for _path, _title_key, _perm in PLACEHOLDER_PAGES:
    router.add_api_route(
        _path,
        _make_placeholder(_title_key, _perm),
        methods=["GET"],
        response_class=HTMLResponse,
        include_in_schema=False,
    )


@router.get("/lang/{code}")
async def switch_language(code: str, request: Request):
    """Set the panel language cookie and bounce back to the referring page."""
    lang = normalize_lang(code) if code in SUPPORTED else None
    target = request.headers.get("referer") or "/"
    resp = RedirectResponse(target, status_code=302)
    if lang:
        resp.set_cookie(LANG_COOKIE, lang, max_age=365 * 24 * 3600, samesite="lax")
    return resp


@router.get("/login", response_class=HTMLResponse)
async def login_page(request: Request, admin: Admin | None = Depends(get_current_admin_optional)):
    if admin is not None:
        return RedirectResponse("/", status_code=302)
    return templates.TemplateResponse("login.html", _ctx(request, None, error=None))


@router.post("/login", response_class=HTMLResponse)
async def login_submit(
    request: Request,
    username: str = Form(...),
    password: str = Form(...),
    session: AsyncSession = Depends(get_session),
):
    # The identifier may be the username or the optional email.
    admin = await authenticate_admin(session, username, password)
    if admin is None:
        lang = _resolve_lang(request)
        return templates.TemplateResponse(
            "login.html",
            _ctx(request, None, error=t("web.invalid_credentials", lang)),
            status_code=401,
        )
    token = create_access_token(admin.id, username=admin.username, email=admin.email)
    resp = RedirectResponse("/", status_code=302)
    set_session_cookie(resp, token, request=request)
    return resp


@router.get("/logout")
async def logout():
    resp = RedirectResponse("/login", status_code=302)
    resp.delete_cookie(COOKIE_NAME)
    return resp


@router.get("/admin")
async def admin_alias():
    """`/admin` is a convenience alias for the panel entry point.

    The panel is served at `/` (which redirects to `/login` when signed out).
    Some operators and older docs reach for `/admin`, so redirect there instead
    of returning 404.
    """
    return RedirectResponse("/", status_code=302)


@router.get("/", response_class=HTMLResponse)
async def dashboard(
    request: Request,
    admin: Admin | None = Depends(get_current_admin_optional),
    session: AsyncSession = Depends(get_session),
):
    if admin is None:
        return RedirectResponse("/login", status_code=302)
    lang = _resolve_lang(request)
    svc = SettingsService(session)
    rows = await svc.all_rows()
    counts: dict[str, int] = {}
    for row in rows:
        meta = DEFAULTS_BY_KEY.get(row.key)
        category = meta.category if meta else "general"
        counts[category] = counts.get(category, 0) + 1
    cats = [
        {
            **meta,
            "title": category_title_for(cat, lang),
            "category": cat,
            "count": counts.get(cat, 0),
        }
        for cat, meta in sorted(CATEGORIES.items(), key=lambda kv: kv[1]["order"])
    ]
    return templates.TemplateResponse(
        "dashboard.html", _ctx(request, admin, categories=cats, total=len(rows))
    )


@router.get("/settings", response_class=HTMLResponse)
async def settings_page(
    request: Request,
    admin: Admin | None = Depends(get_current_admin_optional),
    session: AsyncSession = Depends(get_session),
    saved: int = 0,
    error: str = "",
):
    if admin is None:
        return RedirectResponse("/login", status_code=302)
    lang = _resolve_lang(request)
    if not has_permission(admin.role, "manage_settings"):
        return _forbidden(lang)
    svc = SettingsService(session)
    rows = {r.key: r for r in await svc.all_rows()}

    sections = []
    for cat, meta in sorted(CATEGORIES.items(), key=lambda kv: kv[1]["order"]):
        items = []
        for d in DEFAULTS:
            if d.category != cat:
                continue
            row = rows.get(d.key)
            if d.is_secret:
                value: object = ""  # never render secrets, even seed defaults
            elif row is None:
                value = coerce_out(d.value_type, d.default)
            else:
                value = coerce_out(d.value_type, row.value)
            items.append(
                {
                    "key": d.key,
                    "label": label_for(d, lang),
                    "description": description_for(d, lang),
                    "value_type": d.value_type,
                    "is_secret": d.is_secret,
                    "value": value,
                    "has_secret": bool(row and row.is_secret and row.value),
                }
            )
        if items:
            sections.append(
                {**meta, "title": category_title_for(cat, lang), "category": cat, "items": items}
            )

    return templates.TemplateResponse(
        "settings.html",
        _ctx(request, admin, sections=sections, saved=bool(saved), error=error),
    )


# --------------------------------------------------------------------------
# Products (manage_products RBAC)
# --------------------------------------------------------------------------

def _parse_int_opt(raw: object) -> int | None:
    text = str(raw or "").strip()
    if text == "":
        return None
    return int(text)  # ValueError surfaces to the caller's error handling


def _product_form_values(form: dict[str, object]) -> dict[str, object]:
    """Convert the HTML form payload into ProductCreate/Update field values."""
    return {
        "type": str(form.get("type", "")).strip(),
        "title": str(form.get("title", "")).strip(),
        "description": (str(form.get("description", "")).strip() or None),
        "price": _parse_int_opt(form.get("price")) or 0,
        "duration_days": _parse_int_opt(form.get("duration_days")),
        "traffic_gb": _parse_int_opt(form.get("traffic_gb")),
        "ip_limit": _parse_int_opt(form.get("ip_limit")),
        "server_id": _parse_int_opt(form.get("server_id")),
        "inbound_id": _parse_int_opt(form.get("inbound_id")),
        "is_active": "is_active" in form,
        "is_hidden": "is_hidden" in form,
        "sort_order": _parse_int_opt(form.get("sort_order")) or 0,
    }


@router.get("/products", response_class=HTMLResponse)
async def products_page(
    request: Request,
    admin: Admin | None = Depends(get_current_admin_optional),
    session: AsyncSession = Depends(get_session),
    saved: int = 0,
    error: str = "",
):
    if admin is None:
        return RedirectResponse("/login", status_code=302)
    lang = _resolve_lang(request)
    if not has_permission(admin.role, "manage_products"):
        return _forbidden(lang)
    products = await product_service.list_for_admin(session)
    return templates.TemplateResponse(
        "products.html",
        _ctx(request, admin, products=products, saved=bool(saved), error=error),
    )


@router.get("/products/new", response_class=HTMLResponse)
async def product_new_page(
    request: Request,
    admin: Admin | None = Depends(get_current_admin_optional),
):
    if admin is None:
        return RedirectResponse("/login", status_code=302)
    lang = _resolve_lang(request)
    if not has_permission(admin.role, "manage_products"):
        return _forbidden(lang)
    return templates.TemplateResponse(
        "product_form.html",
        _ctx(request, admin, product=None, product_types=PRODUCT_TYPES, error=""),
    )


@router.post("/products/new")
async def product_create_submit(
    request: Request,
    admin: Admin | None = Depends(get_current_admin_optional),
    session: AsyncSession = Depends(get_session),
):
    if admin is None:
        return RedirectResponse("/login", status_code=302)
    if not has_permission(admin.role, "manage_products"):
        return _forbidden(_resolve_lang(request))
    form = dict(await request.form())
    try:
        data = ProductCreate(**_product_form_values(form))
        await product_service.create(
            session, data, actor_type="admin", actor_id=admin.id
        )
        await session.commit()
    except (ValueError, TypeError) as exc:
        return RedirectResponse(f"/products?error={quote(str(exc))}", status_code=303)
    return RedirectResponse("/products?saved=1", status_code=303)


@router.get("/products/{product_id}/edit", response_class=HTMLResponse)
async def product_edit_page(
    product_id: int,
    request: Request,
    admin: Admin | None = Depends(get_current_admin_optional),
    session: AsyncSession = Depends(get_session),
):
    if admin is None:
        return RedirectResponse("/login", status_code=302)
    lang = _resolve_lang(request)
    if not has_permission(admin.role, "manage_products"):
        return _forbidden(lang)
    product = await product_service.get(session, product_id)
    if product is None:
        return RedirectResponse("/products", status_code=302)
    return templates.TemplateResponse(
        "product_form.html",
        _ctx(request, admin, product=product, product_types=PRODUCT_TYPES, error=""),
    )


@router.post("/products/{product_id}/edit")
async def product_edit_submit(
    product_id: int,
    request: Request,
    admin: Admin | None = Depends(get_current_admin_optional),
    session: AsyncSession = Depends(get_session),
):
    if admin is None:
        return RedirectResponse("/login", status_code=302)
    if not has_permission(admin.role, "manage_products"):
        return _forbidden(_resolve_lang(request))
    form = dict(await request.form())
    try:
        data = ProductUpdate(**_product_form_values(form))
        product = await product_service.update(
            session, product_id, data, actor_type="admin", actor_id=admin.id
        )
        await session.commit()
    except (ValueError, TypeError) as exc:
        return RedirectResponse(f"/products?error={quote(str(exc))}", status_code=303)
    if product is None:
        return RedirectResponse("/products", status_code=302)
    return RedirectResponse("/products?saved=1", status_code=303)


@router.post("/products/{product_id}/toggle-active")
async def product_toggle_active(
    product_id: int,
    request: Request,
    admin: Admin | None = Depends(get_current_admin_optional),
    session: AsyncSession = Depends(get_session),
):
    if admin is None:
        return RedirectResponse("/login", status_code=302)
    if not has_permission(admin.role, "manage_products"):
        return _forbidden(_resolve_lang(request))
    product = await product_service.get(session, product_id)
    if product is not None:
        await product_service.set_active(
            session, product_id, not product.is_active,
            actor_type="admin", actor_id=admin.id,
        )
        await session.commit()
    return RedirectResponse("/products?saved=1", status_code=303)


@router.post("/products/{product_id}/toggle-hidden")
async def product_toggle_hidden(
    product_id: int,
    request: Request,
    admin: Admin | None = Depends(get_current_admin_optional),
    session: AsyncSession = Depends(get_session),
):
    if admin is None:
        return RedirectResponse("/login", status_code=302)
    if not has_permission(admin.role, "manage_products"):
        return _forbidden(_resolve_lang(request))
    product = await product_service.get(session, product_id)
    if product is not None:
        await product_service.set_hidden(
            session, product_id, not product.is_hidden,
            actor_type="admin", actor_id=admin.id,
        )
        await session.commit()
    return RedirectResponse("/products?saved=1", status_code=303)


# --------------------------------------------------------------------------
# 3X-UI servers (manage_xui RBAC)
# --------------------------------------------------------------------------

async def _inbound_counts(session: AsyncSession) -> dict[int, int]:
    """Map server_id -> number of synced inbounds."""
    result = await session.execute(
        select(XuiInbound.server_id, func.count(XuiInbound.id)).group_by(
            XuiInbound.server_id
        )
    )
    return {server_id: count for server_id, count in result.all()}


@router.get("/servers", response_class=HTMLResponse)
async def servers_page(
    request: Request,
    admin: Admin | None = Depends(get_current_admin_optional),
    session: AsyncSession = Depends(get_session),
    saved: int = 0,
    error: str = "",
    tested: str = "",
    synced: str = "",
):
    if admin is None:
        return RedirectResponse("/login", status_code=302)
    lang = _resolve_lang(request)
    if not has_permission(admin.role, "manage_xui"):
        return _forbidden(lang)
    servers = await xui_service.list_servers(session)
    counts = await _inbound_counts(session)
    return templates.TemplateResponse(
        "servers.html",
        _ctx(
            request,
            admin,
            servers=servers,
            counts=counts,
            saved=bool(saved),
            error=error,
            tested=tested,
            synced=synced,
        ),
    )


@router.get("/servers/new", response_class=HTMLResponse)
async def server_new_page(
    request: Request,
    admin: Admin | None = Depends(get_current_admin_optional),
):
    if admin is None:
        return RedirectResponse("/login", status_code=302)
    lang = _resolve_lang(request)
    if not has_permission(admin.role, "manage_xui"):
        return _forbidden(lang)
    return templates.TemplateResponse(
        "server_form.html",
        _ctx(request, admin, versions=SUPPORTED_VERSIONS, error=""),
    )


@router.post("/servers/new")
async def server_create_submit(
    request: Request,
    admin: Admin | None = Depends(get_current_admin_optional),
    session: AsyncSession = Depends(get_session),
):
    if admin is None:
        return RedirectResponse("/login", status_code=302)
    if not has_permission(admin.role, "manage_xui"):
        return _forbidden(_resolve_lang(request))
    form = dict(await request.form())
    web_base_path = str(form.get("web_base_path", "")).strip() or None
    api_token = str(form.get("api_token", "")).strip() or None
    try:
        await xui_service.add_server(
            session,
            name=str(form.get("name", "")).strip(),
            base_url=str(form.get("base_url", "")).strip(),
            username=str(form.get("username", "")).strip(),
            password=str(form.get("password", "")),
            web_base_path=web_base_path,
            panel_version=str(form.get("panel_version", "2.9.4")).strip(),
            api_token=api_token,
            actor_type="admin",
            actor_id=admin.id,
        )
    except (ValueError, TypeError) as exc:
        return RedirectResponse(f"/servers?error={quote(str(exc))}", status_code=303)
    return RedirectResponse("/servers?saved=1", status_code=303)


@router.post("/servers/{server_id}/test")
async def server_test_connection(
    server_id: int,
    request: Request,
    admin: Admin | None = Depends(get_current_admin_optional),
    session: AsyncSession = Depends(get_session),
):
    if admin is None:
        return RedirectResponse("/login", status_code=302)
    lang = _resolve_lang(request)
    if not has_permission(admin.role, "manage_xui"):
        return _forbidden(lang)
    server = await xui_service.get_server(session, server_id)
    if server is None:
        return RedirectResponse("/servers", status_code=303)
    result = await xui_service.test_connection(session, server)
    message = str(result.get("message", ""))
    return RedirectResponse(
        f"/servers?tested={quote(message)}", status_code=303
    )


@router.post("/servers/{server_id}/sync")
async def server_sync_inbounds(
    server_id: int,
    request: Request,
    admin: Admin | None = Depends(get_current_admin_optional),
    session: AsyncSession = Depends(get_session),
):
    if admin is None:
        return RedirectResponse("/login", status_code=302)
    lang = _resolve_lang(request)
    if not has_permission(admin.role, "manage_xui"):
        return _forbidden(lang)
    server = await xui_service.get_server(session, server_id)
    if server is None:
        return RedirectResponse("/servers", status_code=303)
    try:
        count = await xui_service.sync_inbounds(session, server)
    except XuiError as exc:
        return RedirectResponse(f"/servers?error={quote(str(exc))}", status_code=303)
    return RedirectResponse(f"/servers?synced={count}", status_code=303)


@router.post("/servers/{server_id}/delete")
async def server_delete(
    server_id: int,
    request: Request,
    admin: Admin | None = Depends(get_current_admin_optional),
    session: AsyncSession = Depends(get_session),
):
    if admin is None:
        return RedirectResponse("/login", status_code=302)
    if not has_permission(admin.role, "manage_xui"):
        return _forbidden(_resolve_lang(request))
    await xui_service.delete_server(
        session, server_id, actor_type="admin", actor_id=admin.id
    )
    return RedirectResponse("/servers?saved=1", status_code=303)


@router.post("/settings")
async def settings_submit(
    request: Request,
    admin: Admin | None = Depends(get_current_admin_optional),
    session: AsyncSession = Depends(get_session),
):
    if admin is None:
        return RedirectResponse("/login", status_code=302)
    if not has_permission(admin.role, "manage_settings"):
        return _forbidden(_resolve_lang(request))

    form = await request.form()
    values: dict[str, object] = {}
    for d in DEFAULTS:
        if d.value_type == "bool":
            # Unchecked checkboxes are omitted from the form payload.
            values[d.key] = d.key in form
        elif d.key in form:
            submitted = str(form[d.key])
            # A blank secret field means "leave unchanged".
            if d.is_secret and submitted == "":
                continue
            values[d.key] = submitted

    svc = SettingsService(session)
    try:
        await svc.update_many(values, actor_type="admin", actor_id=admin.id)
    except ValueError as exc:
        return RedirectResponse(f"/settings?error={quote(str(exc))}", status_code=303)
    return RedirectResponse("/settings?saved=1", status_code=303)

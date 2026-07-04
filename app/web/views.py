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
from app.schemas.product import ProductCreate, ProductUpdate
from app.services import product_service
from app.web.api.auth import authenticate_admin
from app.web.deps import COOKIE_NAME, get_current_admin_optional, set_session_cookie

BASE_DIR = Path(__file__).resolve().parent
templates = Jinja2Templates(directory=BASE_DIR / "templates")

router = APIRouter(include_in_schema=False)

LANG_COOKIE = "dc_lang"

NAV = [
    {"href": "/", "label_key": "web.nav.dashboard", "icon": "🏠"},
    {"href": "/products", "label_key": "web.nav.products", "icon": "📦"},
    {"href": "/settings", "label_key": "web.nav.settings", "icon": "⚙️"},
]


def _resolve_lang(request: Request) -> str:
    return normalize_lang(request.cookies.get(LANG_COOKIE))


def _ctx(request: Request, admin: Admin | None, **extra: object) -> dict:
    lang = _resolve_lang(request)
    return {
        "request": request,
        "admin": admin,
        "nav": NAV,
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

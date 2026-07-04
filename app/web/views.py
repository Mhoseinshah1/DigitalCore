"""Server-rendered HTML pages for the admin panel.

The panel is server-rendered so the whole stack works with no separate frontend
build — keeping the one-command install genuinely one command. The Settings page
posts a plain form; the JSON API under /api is available for automation.
"""
from __future__ import annotations

from pathlib import Path

from fastapi import APIRouter, Depends, Form, Request
from fastapi.responses import HTMLResponse, RedirectResponse
from fastapi.templating import Jinja2Templates
from sqlalchemy.ext.asyncio import AsyncSession

from app import __version__
from app.core.defaults import CATEGORIES, DEFAULTS, DEFAULTS_BY_KEY
from app.core.security import create_access_token
from app.core.settings_service import SettingsService, coerce_out
from app.database import get_session
from app.models.admin import Admin
from app.web.api.auth import authenticate_admin
from app.web.deps import COOKIE_NAME, get_current_admin_optional, set_session_cookie

BASE_DIR = Path(__file__).resolve().parent
templates = Jinja2Templates(directory=BASE_DIR / "templates")

router = APIRouter(include_in_schema=False)

NAV = [
    {"href": "/", "label": "Dashboard", "icon": "🏠"},
    {"href": "/settings", "label": "Settings", "icon": "⚙️"},
]


def _ctx(request: Request, admin: Admin | None, **extra) -> dict:
    return {
        "request": request,
        "admin": admin,
        "nav": NAV,
        "version": __version__,
        "domain": request.url.hostname,
        **extra,
    }


@router.get("/login", response_class=HTMLResponse)
async def login_page(request: Request, admin: Admin | None = Depends(get_current_admin_optional)):
    if admin is not None:
        return RedirectResponse("/", status_code=302)
    return templates.TemplateResponse("login.html", _ctx(request, None, error=None))


@router.post("/login", response_class=HTMLResponse)
async def login_submit(
    request: Request,
    email: str = Form(...),
    password: str = Form(...),
    session: AsyncSession = Depends(get_session),
):
    admin = await authenticate_admin(session, email, password)
    if admin is None:
        return templates.TemplateResponse(
            "login.html",
            _ctx(request, None, error="Invalid email or password."),
            status_code=401,
        )
    token = create_access_token(admin.id, email=admin.email)
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
    svc = SettingsService(session)
    rows = await svc.all_rows()
    counts: dict[str, int] = {}
    for row in rows:
        meta = DEFAULTS_BY_KEY.get(row.key)
        category = meta.category if meta else "general"
        counts[category] = counts.get(category, 0) + 1
    cats = [
        {**meta, "category": cat, "count": counts.get(cat, 0)}
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
):
    if admin is None:
        return RedirectResponse("/login", status_code=302)
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
                    "label": d.label or d.key,
                    "description": d.description,
                    "value_type": d.value_type,
                    "is_secret": d.is_secret,
                    "value": value,
                    "has_secret": bool(row and row.is_secret and row.value),
                }
            )
        if items:
            sections.append({**meta, "category": cat, "items": items})

    return templates.TemplateResponse(
        "settings.html", _ctx(request, admin, sections=sections, saved=bool(saved))
    )


@router.post("/settings")
async def settings_submit(
    request: Request,
    admin: Admin | None = Depends(get_current_admin_optional),
    session: AsyncSession = Depends(get_session),
):
    if admin is None:
        return RedirectResponse("/login", status_code=302)

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
    await svc.update_many(values)
    return RedirectResponse("/settings?saved=1", status_code=303)

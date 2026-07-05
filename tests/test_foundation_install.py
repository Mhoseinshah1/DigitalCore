"""Foundation install/runtime guards.

These tests exist because a fresh Docker install once broke while pytest stayed
green: `httpx` was imported at startup by the 3X-UI code but only declared in
requirements-dev.txt, so the production image (requirements.txt only) crashed on
import for the backend and the bot. The dependency-completeness test below would
have caught that; the others lock in the /admin alias and the env-var fallbacks.
"""
from __future__ import annotations

import ast
import pathlib
import re
import sys
from importlib.metadata import packages_distributions

import pytest

from app.config import Settings

ROOT = pathlib.Path(__file__).resolve().parent.parent


def _norm(name: str) -> str:
    return re.sub(r"[-_.]+", "-", name).lower()


def _requirements_distributions() -> set[str]:
    """Normalized distribution names pinned in requirements.txt (production)."""
    dists: set[str] = set()
    for raw in (ROOT / "requirements.txt").read_text().splitlines():
        line = raw.split("#", 1)[0].strip()
        if not line:
            continue
        m = re.match(r"^([A-Za-z0-9_.-]+)", line)  # dist name before []/==/;
        if m:
            dists.add(_norm(m.group(1)))
    return dists


def _thirdparty_imports_under_app() -> set[str]:
    """Every top-level third-party module imported anywhere under app/."""
    stdlib = set(sys.stdlib_module_names)
    mods: set[str] = set()
    for path in (ROOT / "app").rglob("*.py"):
        tree = ast.parse(path.read_text(), filename=str(path))
        for node in ast.walk(tree):
            if isinstance(node, ast.Import):
                for alias in node.names:
                    mods.add(alias.name.split(".")[0])
            elif isinstance(node, ast.ImportFrom) and node.level == 0 and node.module:
                mods.add(node.module.split(".")[0])
    return {m for m in mods if m not in stdlib and m != "app"}


def test_all_app_imports_are_declared_in_requirements() -> None:
    """Regression: every runtime import in app/ must be a production dependency.

    Guards against the httpx-style failure where a startup import lived only in
    requirements-dev.txt and the production image crashed on boot.
    """
    req = _requirements_distributions()
    pkg_dists = packages_distributions()
    missing: list[tuple[str, object]] = []
    for mod in sorted(_thirdparty_imports_under_app()):
        dists = {_norm(d) for d in pkg_dists.get(mod, [])}
        if not dists or not (dists & req):
            missing.append((mod, sorted(dists) or "not-installed"))
    assert not missing, (
        "app/ imports a runtime module not declared in requirements.txt: "
        f"{missing}. Add the providing distribution to requirements.txt."
    )


def test_httpx_is_a_production_dependency() -> None:
    """httpx is imported at startup by app/xui/* (backend + bot both load it)."""
    assert "httpx" in _requirements_distributions()


async def test_admin_route_redirects_not_404(client) -> None:
    """/admin must be reachable (redirect), never a 404 'panel does not open'."""
    r = await client.get("/admin", follow_redirects=False)
    assert r.status_code in (302, 307)
    assert r.headers["location"] == "/"


def _settings_with(env: dict[str, str]) -> Settings:
    # _env_file=None isolates the test from any local .env; env dict is the input.
    import os

    for key in ("TELEGRAM_BOT_TOKEN", "BOT_TOKEN", "TELEGRAM_ADMIN_ID",
                "MAIN_ADMIN_TELEGRAM_ID"):
        os.environ.pop(key, None)
    os.environ.update(env)
    try:
        return Settings(_env_file=None)
    finally:
        for key in env:
            os.environ.pop(key, None)


def test_env_fallback_bot_token_alias() -> None:
    assert _settings_with({"BOT_TOKEN": "abc123"}).TELEGRAM_BOT_TOKEN == "abc123"
    assert _settings_with({"TELEGRAM_BOT_TOKEN": "canon"}).TELEGRAM_BOT_TOKEN == "canon"
    # Canonical name wins when both are present.
    both = _settings_with({"TELEGRAM_BOT_TOKEN": "canon", "BOT_TOKEN": "alias"})
    assert both.TELEGRAM_BOT_TOKEN == "canon"


def test_env_fallback_admin_id_alias() -> None:
    assert _settings_with({"MAIN_ADMIN_TELEGRAM_ID": "777"}).TELEGRAM_ADMIN_ID == 777
    assert _settings_with({"TELEGRAM_ADMIN_ID": "888"}).TELEGRAM_ADMIN_ID == 888
    assert _settings_with({}).TELEGRAM_ADMIN_ID is None

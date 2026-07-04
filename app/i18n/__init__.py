"""Bilingual (fa/en) i18n layer.

Usage:
    from app.i18n import t, is_rtl, DEFAULT_LANG, SUPPORTED
    t("greeting", "en")                  -> "👋 Welcome to DigitalCore!"
    t("settings.updated", "fa", label=…) -> formatted Persian string

Lookup order: requested language -> fa (default) -> the key itself. Formatting
params are applied with str.format; a formatting mismatch returns the raw
template rather than raising (user output must never crash a handler).

Only user-facing strings live here. Code, logs, and exceptions stay English.
"""
from __future__ import annotations

import logging

from app.i18n.en import CATALOG as EN
from app.i18n.fa import CATALOG as FA

log = logging.getLogger("i18n")

DEFAULT_LANG = "fa"
SUPPORTED: tuple[str, ...] = ("fa", "en")

_CATALOGS: dict[str, dict[str, str]] = {"fa": FA, "en": EN}

_RTL_LANGS = frozenset({"fa"})


def normalize_lang(lang: str | None) -> str:
    """Coerce any input to a supported language code (default fa)."""
    if lang:
        code = str(lang).strip().lower()[:2]
        if code in SUPPORTED:
            return code
    return DEFAULT_LANG


def is_rtl(lang: str | None) -> bool:
    """True for right-to-left languages (fa)."""
    return normalize_lang(lang) in _RTL_LANGS


def t(key: str, lang: str | None = None, **params: object) -> str:
    """Translate `key` into `lang`, formatting with `params`.

    Falls back to the fa catalog, then to the key itself when missing.
    """
    code = normalize_lang(lang)
    template = _CATALOGS[code].get(key)
    if template is None and code != DEFAULT_LANG:
        template = _CATALOGS[DEFAULT_LANG].get(key)
    if template is None:
        log.warning("Missing i18n key: %s", key)
        return key
    if not params:
        return template
    try:
        return template.format(**params)
    except (KeyError, IndexError, ValueError):
        log.warning("Bad i18n params for key %s", key)
        return template


def texts_for(key: str) -> set[str]:
    """All translations of a key — for matching button presses in any language."""
    return {t(key, lang) for lang in SUPPORTED}

"""i18n layer: catalog parity, translation, fallbacks, RTL detection."""
from __future__ import annotations

from app.i18n import DEFAULT_LANG, SUPPORTED, is_rtl, normalize_lang, t, texts_for
from app.i18n.en import CATALOG as EN
from app.i18n.fa import CATALOG as FA


def test_catalogs_have_identical_key_sets() -> None:
    """CI guard: the fa and en catalogs must never drift apart."""
    fa_keys = set(FA)
    en_keys = set(EN)
    missing_in_en = sorted(fa_keys - en_keys)
    missing_in_fa = sorted(en_keys - fa_keys)
    assert not missing_in_en, f"keys missing in en catalog: {missing_in_en}"
    assert not missing_in_fa, f"keys missing in fa catalog: {missing_in_fa}"


def test_defaults() -> None:
    assert DEFAULT_LANG == "fa"
    assert SUPPORTED == ("fa", "en")


def test_t_returns_right_language() -> None:
    assert t("greeting", "en") == "👋 Welcome to DigitalCore!"
    assert t("greeting", "fa") == "👋 به دیجیتال‌کور خوش آمدید!"


def test_t_formats_params() -> None:
    out = t("admin.panel_title", "en", role="owner")
    assert "owner" in out
    out_fa = t("settings.updated", "fa", label="X", value="Y")
    assert "X" in out_fa and "Y" in out_fa


def test_t_falls_back_to_fa_then_key() -> None:
    # Unknown language falls back to the default (fa).
    assert t("greeting", "de") == t("greeting", "fa")
    # Missing key returns the key itself.
    assert t("no.such.key", "en") == "no.such.key"
    assert t("no.such.key", "fa") == "no.such.key"


def test_is_rtl() -> None:
    assert is_rtl("fa") is True
    assert is_rtl("en") is False
    assert is_rtl(None) is True  # default language is fa


def test_normalize_lang() -> None:
    assert normalize_lang("EN") == "en"
    assert normalize_lang("fa-IR") == "fa"
    assert normalize_lang("de") == "fa"
    assert normalize_lang(None) == "fa"


def test_texts_for_covers_both_languages() -> None:
    texts = texts_for("btn.rules")
    assert t("btn.rules", "fa") in texts
    assert t("btn.rules", "en") in texts

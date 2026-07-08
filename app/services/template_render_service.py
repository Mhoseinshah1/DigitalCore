"""Safe {variable} rendering for admin-configurable bot texts (Payment Core).

Admins edit invoice / receipt templates containing `{username}`-style variables.
This renderer substitutes ONLY the placeholders present in the provided mapping
— no eval, no format-spec tricks (`{x!r}`, `{x:>10}` stay untouched), and an
unknown variable is left visible so a typo in a template is easy to spot
instead of silently vanishing or crashing the bot.
"""
from __future__ import annotations

import re
from typing import Any, Mapping

# {variable_name} — letters/digits/underscore only; anything fancier (format
# specs, nested braces) is intentionally NOT a placeholder.
_VAR_RE = re.compile(r"\{([A-Za-z_][A-Za-z0-9_]*)\}")

# The documented template vocabulary (kept in one place for the admin UI/docs).
KNOWN_VARIABLES: tuple[str, ...] = (
    "username", "full_name", "telegram_id",
    "name_product", "Service_time", "price", "Volume", "note", "userBalance",
    "card_number", "name_card",
    "tracking_code", "invoice_number", "order_number",
    "final_price", "discount", "category", "location", "limit_user",
)


def render_text_template(template: str, variables: Mapping[str, Any]) -> str:
    """Replace `{name}` placeholders from `variables`; unknown ones stay visible.

    Values render via `str()`; a None value renders as an empty string so a
    missing optional field never shows the literal word "None".
    """
    if not template:
        return ""

    def _sub(match: re.Match[str]) -> str:
        name = match.group(1)
        if name not in variables:
            return match.group(0)  # unknown -> keep {name} visible
        value = variables[name]
        return "" if value is None else str(value)

    return _VAR_RE.sub(_sub, str(template))


def format_toman(amount: int | None) -> str:
    """`1234567 -> '1,234,567'` — the money formatting used inside templates."""
    return f"{int(amount or 0):,}"

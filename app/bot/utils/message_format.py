"""Consistent, readable Telegram message formatting.

The owner asked for larger, clearer bot messages (title line, a divider, blank
lines between sections, label/value on their own lines) instead of dense
one-liners. These helpers give every message the same look.

All output is HTML-parse-mode safe: :func:`safe_code` (and any value you pass
through it) is escaped, so callers wrapping user-controlled text won't break
Telegram's HTML parser. Plain section values are NOT auto-escaped — pass them
through :func:`safe_code`/:func:`esc` when they may contain ``< > &``.
"""
from __future__ import annotations

from collections.abc import Iterable, Sequence
from html import escape

# A visually distinct horizontal rule (used across every "big" message).
DIVIDER = "━━━━━━━━━━━━━━━━━━"

# Status → emoji marker. Covers order/payment/service statuses the bot renders.
_STATUS_EMOJI: dict[str, str] = {
    "active": "🟢", "delivered": "✅", "approved": "✅", "paid": "✅",
    "pending": "⏳", "pending_payment": "⏳", "waiting_admin": "🕓",
    "receipt_submitted": "🕓", "processing": "⏳", "provisioning": "⏳",
    "rejected": "❌", "failed": "❌", "cancelled": "🚫", "canceled": "🚫",
    "expired": "⌛", "disabled": "⛔", "inactive": "⚪", "hidden": "🙈",
    "refunded": "↩️",
}


def divider() -> str:
    """The standard section separator line."""
    return DIVIDER


def esc(value: object) -> str:
    """HTML-escape a value for safe inclusion in an HTML-parse-mode message."""
    return escape(str(value if value is not None else ""))


def safe_code(value: object) -> str:
    """Escaped value wrapped in a monospace ``<code>`` span (tap-to-copy)."""
    return f"<code>{esc(value)}</code>"


def section_title(title: str) -> str:
    """A title line immediately followed by a divider."""
    return f"{title}\n{DIVIDER}"


def format_money(amount: object, *, unit: str = "تومان") -> str:
    """A comma-grouped monetary value, e.g. ``1,234,000 تومان``."""
    try:
        n = int(amount or 0)
    except (TypeError, ValueError):
        n = 0
    return f"{n:,} {unit}".strip()


def format_gb(value: object, *, unit: str = "گیگابایت", unlimited: str = "نامحدود") -> str:
    """A human traffic size. ``0``/``None`` → *unlimited*; very large inputs are
    treated as raw bytes and converted to GB."""
    try:
        n = float(value or 0)
    except (TypeError, ValueError):
        n = 0
    if n <= 0:
        return unlimited
    if n >= 1_000_000:  # looks like a byte count, not a GB count
        n = n / (1024 ** 3)
    text = f"{n:.0f}" if float(n).is_integer() else f"{n:.2f}".rstrip("0").rstrip(".")
    return f"{text} {unit}"


def format_status_badge(status: object, label: str | None = None) -> str:
    """An emoji badge for a status, e.g. ``🟢 active`` (or a supplied label)."""
    key = str(status or "").strip().lower()
    emoji = _STATUS_EMOJI.get(key, "•")
    return f"{emoji} {label if label is not None else status}"


def render_big_message(
    title: str,
    *,
    sections: Sequence[tuple[str, object]] | None = None,
    lines: Iterable[str] | None = None,
    footer: str | None = None,
) -> str:
    """Compose a large, readable message.

    ``title``    → the header line (add your own emoji), followed by a divider.
    ``sections`` → ``(label, value)`` pairs rendered as ``label`` then ``value``
                   on the next line, with a blank line between pairs. A pair
                   whose value is None/"" is skipped.
    ``lines``    → extra plain lines appended after the sections.
    ``footer``   → a closing note preceded by a divider.
    """
    out: list[str] = [section_title(title)]
    if sections:
        blocks = [f"{label}\n{value}" for label, value in sections
                  if value is not None and str(value) != ""]
        if blocks:
            out.append("")
            out.append("\n\n".join(blocks))
    if lines:
        extra = [ln for ln in lines]
        if extra:
            out.append("")
            out.extend(extra)
    if footer:
        out.append("")
        out.append(DIVIDER)
        out.append(footer)
    return "\n".join(out)

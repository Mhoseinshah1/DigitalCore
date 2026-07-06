"""Subscription links + QR codes for provisioned V2Ray services (Phase 6).

Conservative by design: a subscription URL is built ONLY when an admin has
configured the server's public subscription host (`public_sub_base_url`). We
never derive it from the admin-panel `base_url` — that host/port is usually the
private panel, not the public subscription endpoint. When it can't be built the
caller stores null and the user is told support will follow up.

QR generation is best-effort: `qrcode`/Pillow are imported lazily, and any
failure returns None so provisioning never breaks over a missing image lib.
"""
from __future__ import annotations

import logging
import re
from pathlib import Path

log = logging.getLogger("subscription")

# storage/exports/qrcodes/ at the repo root. Overridable in tests via monkeypatch.
QR_ROOT: Path = Path(__file__).resolve().parents[2] / "storage" / "exports" / "qrcodes"


def build_subscription_url(server, sub_id: str | None) -> str | None:
    """`{public_sub_base_url}{subscription_path}{sub_id}` or None if unconfigured.

    Requires the server's `public_sub_base_url` to be set (the public
    subscription host). `subscription_path` defaults to ``/sub/``. Returns None
    when either the sub_id or the public host is missing — the admin must
    configure the subscription host before links can be generated.
    """
    if not sub_id:
        return None
    base = (getattr(server, "public_sub_base_url", None) or "").strip().rstrip("/")
    if not base:
        return None
    path = (getattr(server, "subscription_path", None) or "/sub/").strip()
    if not path.startswith("/"):
        path = "/" + path
    if not path.endswith("/"):
        path = path + "/"
    return f"{base}{path}{sub_id}"


def _safe_name(service_id: int) -> str:
    return re.sub(r"[^A-Za-z0-9_-]", "", f"service_{int(service_id)}") + ".png"


def generate_qr_png(text: str, service_id: int) -> str | None:
    """Render `text` as a QR PNG under QR_ROOT; return its path, or None.

    Never raises: a missing `qrcode`/Pillow, or any render/IO error, degrades to
    a text-only delivery (returns None). The returned path is absolute.
    """
    if not text:
        return None
    try:
        import qrcode  # lazy: optional dependency
    except Exception as exc:  # noqa: BLE001
        log.info("QR generation skipped (qrcode unavailable): %s", exc)
        return None
    try:
        QR_ROOT.mkdir(parents=True, exist_ok=True)
        dest = QR_ROOT / _safe_name(service_id)
        img = qrcode.make(text)
        img.save(dest)
        return str(dest)
    except Exception as exc:  # noqa: BLE001
        log.warning("QR generation failed for service %s: %s", service_id, exc)
        return None

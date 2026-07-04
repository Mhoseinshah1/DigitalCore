"""Version-selectable adapter registry.

Maps (panel_type, panel_version) -> adapter class and builds a ready PanelAdapter
for a stored XuiServer (decrypting its credentials via app/core/crypto.py).
"""
from __future__ import annotations

from typing import TYPE_CHECKING

import httpx

from app.xui.adapters.xui_2_9_4 import Xui294Adapter
from app.xui.adapters.xui_latest import XuiLatestAdapter
from app.xui.base import PanelAdapter
from app.xui.client import XuiHttpClient, SleepFn
from app.xui.exceptions import XuiApiError

if TYPE_CHECKING:  # avoid importing ORM/crypto at module import time
    from app.models.xui_server import XuiServer

PANEL_ADAPTERS: dict[tuple[str, str], type[PanelAdapter]] = {
    ("3x-ui", "2.9.4"): Xui294Adapter,
    ("3x-ui", "latest"): XuiLatestAdapter,
}

SUPPORTED_VERSIONS: tuple[str, ...] = ("2.9.4", "latest")


def get_adapter_class(panel_type: str, panel_version: str) -> type[PanelAdapter]:
    key = (panel_type, panel_version)
    adapter_cls = PANEL_ADAPTERS.get(key)
    if adapter_cls is None:
        raise XuiApiError(f"unsupported panel {panel_type!r} version {panel_version!r}")
    return adapter_cls


def get_adapter(
    server: "XuiServer",
    *,
    transport: httpx.BaseTransport | None = None,
    sleep: SleepFn | None = None,
) -> PanelAdapter:
    """Build a PanelAdapter for a stored server, decrypting its credentials.

    `transport` / `sleep` are injection points for tests (mocked HTTP, no waits).
    """
    from app.core import crypto  # local import: keep app.xui import-light

    adapter_cls = get_adapter_class(server.panel_type, server.panel_version)
    password = crypto.decrypt(server.encrypted_password) if server.encrypted_password else ""
    kwargs: dict[str, object] = {
        "base_url": server.base_url,
        "username": server.username,
        "password": password,
        "web_base_path": server.web_base_path,
        "transport": transport,
    }
    if sleep is not None:
        kwargs["sleep"] = sleep
    http = XuiHttpClient(**kwargs)  # type: ignore[arg-type]
    return adapter_cls(http)

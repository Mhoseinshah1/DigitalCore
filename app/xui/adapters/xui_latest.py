"""3X-UI "latest" adapter.

Subclasses the v2.9.4 adapter and overrides ONLY what differs in the newest
3X-UI release. Where a difference is unconfirmed, the 2.9.4 behaviour is kept
and a TODO marks it for verification against a real panel — nothing is guessed
silently.

Known/likely candidates to confirm against a live latest panel:
  - TODO(latest): confirm the delete-client path. Some newer builds expose
    POST {base}/panel/api/inbounds/delClient/{inboundId}/{clientUuid} (uuid,
    not email) rather than 2.9.4's {inboundId}/delClient/{clientUuidOrEmail}.
  - TODO(latest): confirm getClientTraffics still returns a single obj (some
    builds return a list) and whether a getClientTrafficsById variant exists.
  - TODO(latest): confirm addClient/updateClient still take the JSON-string
    `settings` field (unchanged through 2.9.x) and no new required fields.
Until confirmed, every operation inherits the 2.9.4 implementation verbatim.
"""
from __future__ import annotations

from app.xui.adapters.xui_2_9_4 import Xui294Adapter


class XuiLatestAdapter(Xui294Adapter):
    panel_type = "3x-ui"
    panel_version = "latest"

    # No overrides yet — see the module docstring for the TODO list of endpoints
    # to confirm before diverging from 2.9.4.

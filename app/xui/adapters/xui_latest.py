"""3X-UI "latest" adapter.

Subclasses the v2.9.4 adapter. The current MHSanaei/3x-ui API keeps the same
inbound/client route shapes 2.9.4 uses, so every operation is inherited verbatim.
Confirmed against the latest API (MHSanaei/3x-ui panel controller routes):

  - list/get inbound:  GET  /panel/api/inbounds/list, GET /panel/api/inbounds/get/{id}
  - addClient:         POST /panel/api/inbounds/addClient   (form: id, settings-JSON)
  - updateClient:      POST /panel/api/inbounds/updateClient/{clientUuid}
  - delClient:         POST /panel/api/inbounds/{inboundId}/delClient/{clientId}
  - resetClientTraffic:POST /panel/api/inbounds/{inboundId}/resetClientTraffic/{email}
  - getClientTraffics: GET  /panel/api/inbounds/getClientTraffics/{email}  (single obj)

The `settings` JSON string and its client fields (id/email/enable/expiryTime[ms]/
totalGB[bytes]/limitIp/subId/tgId) are unchanged, so no field overrides are
needed. Should a future release diverge, override only the differing path/parse
here — nothing is guessed.
"""
from __future__ import annotations

from app.xui.adapters.xui_2_9_4 import Xui294Adapter


class XuiLatestAdapter(Xui294Adapter):
    panel_type = "3x-ui"
    panel_version = "latest"

    # Endpoints confirmed identical to 2.9.4 against the latest API (see docstring).

"""Typed errors for the 3X-UI integration. Messages stay English (internal)."""
from __future__ import annotations


class XuiError(Exception):
    """Base class for every 3X-UI integration error."""


class XuiAuthError(XuiError):
    """Login failed or the session expired and could not be renewed."""


class XuiNotFoundError(XuiError):
    """The requested inbound/client does not exist on the panel."""


class XuiApiError(XuiError):
    """The panel replied with success=false or an otherwise invalid payload."""


class XuiNetworkError(XuiError):
    """The panel was unreachable after all retries (timeout / connection / 5xx)."""


class XuiVerificationError(XuiError):
    """A verify-after-write read did not match what was written."""

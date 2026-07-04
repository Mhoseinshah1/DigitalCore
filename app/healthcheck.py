"""Container healthcheck: verifies the backend answers on /health."""
from __future__ import annotations

import sys
import urllib.request

from app.config import settings


def main() -> None:
    url = f"http://127.0.0.1:{settings.API_PORT}/health"
    try:
        with urllib.request.urlopen(url, timeout=3) as resp:
            sys.exit(0 if resp.status == 200 else 1)
    except Exception:  # noqa: BLE001
        sys.exit(1)


if __name__ == "__main__":
    main()

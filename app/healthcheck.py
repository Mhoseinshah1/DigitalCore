"""Container healthcheck: verifies the web panel answers on the health endpoint."""
from __future__ import annotations

import sys
import urllib.request


def main() -> None:
    try:
        with urllib.request.urlopen("http://127.0.0.1:8000/healthz", timeout=3) as resp:
            sys.exit(0 if resp.status == 200 else 1)
    except Exception:  # noqa: BLE001
        sys.exit(1)


if __name__ == "__main__":
    main()

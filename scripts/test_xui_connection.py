#!/usr/bin/env python
"""Probe a stored 3X-UI server's connection from the command line.

Runs the same rich connection test the admin panel uses (auth + server status +
inbounds) against a real panel and prints a secret-free diagnostic report. Handy
for verifying a newly-added server, or debugging a panel the bot can't reach.

No credential, token, cookie, or subscription URL is ever printed.

Usage (inside the backend container, or with the app env loaded):
    python scripts/test_xui_connection.py --server-id 1
    python scripts/test_xui_connection.py --server-id 1 --sync
    python scripts/test_xui_connection.py --all
"""
from __future__ import annotations

import argparse
import asyncio
import os
import sys

# Allow running as `python scripts/test_xui_connection.py` from the repo root.
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from app.database import SessionLocal  # noqa: E402
from app.services import xui_server_service  # noqa: E402


def _fmt(value: object) -> str:
    return "—" if value is None else str(value)


async def _probe_one(session, server, do_sync: bool) -> bool:
    print(f"\n=== server #{server.id}: {server.name} ({server.base_url}) ===")
    print(f"    auth mode : {server.auth_mode}")
    result = await xui_server_service.test_connection(session, server.id)
    ok = bool(result.get("ok"))
    mark = "✅" if ok else "❌"
    print(f"{mark} connection : {'ok' if ok else 'FAILED'}")
    print(f"    status     : {_fmt(result.get('status'))}")
    print(f"    message    : {_fmt(result.get('message'))}")
    print(f"    inbounds   : {_fmt(result.get('inbound_count'))}")
    print(f"    panel ver  : {_fmt(result.get('panel_version'))}")
    print(f"    xray ver   : {_fmt(result.get('xray_version'))}")

    if do_sync and ok:
        sync = await xui_server_service.sync_inbounds(session, server.id)
        smark = "✅" if sync.get("ok") else "❌"
        print(f"{smark} sync       : {_fmt(sync.get('message'))}")
    return ok


async def run(server_id: int | None, do_all: bool, do_sync: bool) -> int:
    async with SessionLocal() as session:
        if do_all:
            servers = await xui_server_service.list_servers(session)
            if not servers:
                print("No 3X-UI servers configured.")
                return 1
            results = [await _probe_one(session, s, do_sync) for s in servers]
            print(f"\n{sum(results)}/{len(results)} server(s) reachable.")
            return 0 if all(results) else 2

        server = await xui_server_service.get_server(session, server_id)
        if server is None:
            print(f"ERROR: no server with id {server_id}.")
            return 1
        ok = await _probe_one(session, server, do_sync)
        return 0 if ok else 2


def main() -> None:
    parser = argparse.ArgumentParser(description="Test a 3X-UI server connection.")
    group = parser.add_mutually_exclusive_group(required=True)
    group.add_argument("--server-id", type=int, help="ID of the stored server to test.")
    group.add_argument("--all", action="store_true", help="Test every stored server.")
    parser.add_argument("--sync", action="store_true",
                        help="Also sync inbounds when the connection succeeds.")
    args = parser.parse_args()
    sys.exit(asyncio.run(run(args.server_id, args.all, args.sync)))


if __name__ == "__main__":
    main()

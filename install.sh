#!/usr/bin/env bash
# Convenience forwarder. The production installer lives at scripts/install.sh;
# running ./install.sh from the repo root delegates to it (all flags/env passed
# through). See scripts/install.sh for the full, hardened logic.
set -Eeuo pipefail
SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
exec "$SCRIPT_DIR/scripts/install.sh" "$@"

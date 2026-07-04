#!/usr/bin/env bash
# Convenience forwarder. The Phase 1 installer lives at scripts/install.sh.
# Running ./install.sh from the repo root delegates to it.
set -euo pipefail
SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
exec "$SCRIPT_DIR/scripts/install.sh" "$@"

#!/usr/bin/env bash
# Convenience forwarder so `scripts/install.sh` also works.
# The real one-command installer lives at the repository root (./install.sh);
# this simply delegates to it, passing along any arguments.
set -euo pipefail
SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
exec "$SCRIPT_DIR/../install.sh" "$@"

#!/usr/bin/env bash
# Backwards-compatible wrapper delegating to the unified verification gate (`scripts/gate.sh`).
set -euo pipefail

exec "$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)/gate.sh" "$@"

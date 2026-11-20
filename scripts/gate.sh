#!/usr/bin/env bash
# Canonical verification gate for fleetforge.
set -euo pipefail

REPO_ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
cd "${REPO_ROOT}"

export PYTHONDONTWRITEBYTECODE=1

if [[ $# -eq 0 ]]; then
  uv run --no-sync ruff check --no-cache .
  uv run --no-sync ruff format --check --no-cache .
  uv run --no-sync mypy --cache-dir=/dev/null src/
  exec uv run --no-sync pytest -p no:cacheprovider -k "not (TestCiCdDatabaseAndApi or TestTrack1 and database)"
else
  exec uv run --no-sync pytest -p no:cacheprovider "$@"
fi

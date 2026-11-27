#!/usr/bin/env bash
# Single source-of-truth verification gate for fleetforge.
#
# Full gate (no args):
#   - refuses FF_ALLOW_DB_SKIP=1 under CI
#   - checks uv.lock offline + single Alembic head
#   - runs ruff check, ruff format --check, and mypy across src/ tests/ alembic/ agent/tools/ scripts/
#   - runs full pytest (-q -rs -p no:cacheprovider) and validates the strict skip allowlist
#   - runs frontend typecheck + vitest when npm & node_modules are present (unless FF_SKIP_FRONTEND_GATE=1)
#
# Pass arguments to run targeted pytest selections directly (e.g. `gate test fleetforge tests/test_cli.py`).
set -euo pipefail

REPO_ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
cd "${REPO_ROOT}"

export PYTHONDONTWRITEBYTECODE=1
export PYTHONPATH="${REPO_ROOT}/src:${REPO_ROOT}"
if [[ -d "${HOME}/.local/bin" ]]; then
  export PATH="${HOME}/.local/bin:${PATH}"
fi

CI_LOWER="$(printf '%s' "${CI:-}" | tr '[:upper:]' '[:lower:]')"
if [[ -n "${CI_LOWER}" && "${CI_LOWER}" != "0" && "${CI_LOWER}" != "false" && "${FF_ALLOW_DB_SKIP:-0}" == "1" ]]; then
  echo "ERROR: FF_ALLOW_DB_SKIP=1 is forbidden when CI is set (CI=${CI})" >&2
  exit 1
fi

PYTHON_BIN="${REPO_ROOT}/.venv/bin/python"
RUFF_BIN="${REPO_ROOT}/.venv/bin/ruff"
MYPY_BIN="${REPO_ROOT}/.venv/bin/mypy"
PYTEST_BIN="${REPO_ROOT}/.venv/bin/pytest"

if [[ $# -gt 0 ]]; then
  exec "${PYTEST_BIN}" -p no:cacheprovider "$@"
fi

uv lock --check --offline --no-config

"${PYTHON_BIN}" -c '
from alembic.config import Config
from alembic.script import ScriptDirectory
heads = ScriptDirectory.from_config(Config("alembic.ini")).get_heads()
assert len(heads) == 1, f"expected a single Alembic head, found {heads}"
'

"${PYTHON_BIN}" scripts/generate_openapi_ts.py --check

"${RUFF_BIN}" check --no-cache src/ tests/ alembic/ agent/tools/ scripts/
"${RUFF_BIN}" format --check --no-cache src/ tests/ alembic/ agent/tools/ scripts/
"${MYPY_BIN}" --config-file pyproject.toml --cache-dir=/tmp/fleetforge-mypy-cache src/ tests/ alembic/ agent/tools/ scripts/

PYTEST_OUT="$(mktemp /tmp/fleetforge-pytest.XXXXXX)"
trap 'rm -f "${PYTEST_OUT}"' EXIT
XDIST_ARGS=()
if [[ -n "${FF_PYTEST_WORKERS:-}" ]]; then
  XDIST_ARGS=(-n "${FF_PYTEST_WORKERS}")
fi
"${PYTEST_BIN}" -q -rs --durations=15 -p no:cacheprovider "${XDIST_ARGS[@]}" --cov=fleetforge --cov-report=term-missing:skip-covered | tee "${PYTEST_OUT}"

"${PYTHON_BIN}" scripts/check_pytest_skips.py "${PYTEST_OUT}"

if [[ "${FF_SKIP_FRONTEND_GATE:-0}" != "1" ]] && command -v npm >/dev/null 2>&1 && [[ -x "${REPO_ROOT}/frontend/node_modules/.bin/tsc" ]]; then
  (
    cd "${REPO_ROOT}/frontend"
    npm run typecheck
    npm run test:coverage
    npm run build
  )
fi

#!/usr/bin/env bash
set -euo pipefail

REPO_ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
cd "${REPO_ROOT}"

export PYTHONDONTWRITEBYTECODE=1
export PYTHONPATH="${REPO_ROOT}/src:${REPO_ROOT}"
if [[ -d "${HOME}/.local/bin" ]]; then
  export PATH="${HOME}/.local/bin:${PATH}"
fi

uv lock --check --offline --no-config

"${REPO_ROOT}/.venv/bin/python" -c '
from alembic.config import Config
from alembic.script import ScriptDirectory
heads = ScriptDirectory.from_config(Config("alembic.ini")).get_heads()
assert len(heads) == 1, f"expected a single Alembic head, found {heads}"
'

"${REPO_ROOT}/.venv/bin/ruff" check --no-cache src/ tests/ alembic/ agent/tools/ scripts/
"${REPO_ROOT}/.venv/bin/ruff" format --check src/ tests/ alembic/ agent/tools/ scripts/
"${REPO_ROOT}/.venv/bin/mypy" --config-file pyproject.toml --cache-dir=/tmp/top3-mypy-fleetforge src/ tests/ agent/tools/ scripts/

PYTEST_OUT="$(mktemp /tmp/fleetforge-pytest.XXXXXX)"
trap 'rm -f "${PYTEST_OUT}"' EXIT
"${REPO_ROOT}/.venv/bin/pytest" -q -rs -p no:cacheprovider | tee "${PYTEST_OUT}"

if grep -Eq "PostgreSQL unreachable" "${PYTEST_OUT}"; then
  echo "ERROR: database-backed tests were skipped because PostgreSQL is unreachable" >&2
  exit 1
fi

if [[ -x "${REPO_ROOT}/frontend/node_modules/.bin/tsc" ]]; then
  (
    cd "${REPO_ROOT}/frontend"
    npm run typecheck
    npm test
  )
fi

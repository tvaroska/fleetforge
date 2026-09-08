#!/bin/sh
# Container entrypoint for both fleetforge processes.
#
# RUN_MIGRATIONS=true is set on the `api` service only — the ingestor must never
# migrate, or two processes race `alembic upgrade head` on every restart.
# design/production.md -> Components: "Alembic, RUN_MIGRATIONS=true, bingo's pattern".
set -e

if [ "${RUN_MIGRATIONS:-false}" = "true" ]; then
    echo "entrypoint: alembic upgrade head"
    alembic upgrade head
fi

exec "$@"

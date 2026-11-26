#!/bin/sh
# Container entrypoint for both fleetforge processes.
#
# RUN_MIGRATIONS=true is set on the `api` service only (never `ingestor`).
# Multi-replica `api` rollouts (`replicas: 2`, `maxSurge: 1`) are safe because
# `alembic/env.py` serializes online migrations behind a PostgreSQL session
# advisory lock (`pg_advisory_lock`).
# design/production.md -> Components: "Alembic, RUN_MIGRATIONS=true, bingo's pattern".
set -e

if [ "${RUN_MIGRATIONS:-false}" = "true" ]; then
    echo "entrypoint: alembic upgrade head (advisory-locked)"
    alembic upgrade head
fi

exec "$@"

"""External container build worker CLI (`python -m fleetforge.builder`) (`R10-1`).

Polls `build_jobs` rows in state `queued` (`FOR UPDATE SKIP LOCKED`) and compiles
firmware in ephemeral single-tenant ESP-IDF containers outside the `fleetforge-api` pod.
"""

from __future__ import annotations

import argparse
import asyncio
import uuid
from collections.abc import Sequence

from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from fleetforge.builds import (
    BuildJob,
    BuildRunner,
    GitShallowSourceFetcher,
    SourceFetcher,
    create_build_runner,
    execute_build_job,
)
from fleetforge.config import Settings, get_settings
from fleetforge.db.base import get_sessionmaker
from fleetforge.storage.factory import create_object_store
from fleetforge.storage.objectstore import ObjectStore

_NEXT_QUEUED_BUILD_SQL = text(
    """
    SELECT id
      FROM build_jobs
     WHERE status = 'queued'
     ORDER BY created_at ASC, id ASC
     FOR UPDATE SKIP LOCKED
     LIMIT 1
    """
)


async def run_once(
    sessionmaker: async_sessionmaker[AsyncSession],
    store: ObjectStore,
    settings: Settings,
    *,
    source_fetcher: SourceFetcher | None = None,
    build_runner: BuildRunner | None = None,
    max_jobs: int = 8,
) -> list[BuildJob]:
    """Execute up to `max_jobs` queued build jobs sequentially and return their final rows."""
    fetcher = source_fetcher or GitShallowSourceFetcher()
    runner = build_runner or create_build_runner()
    completed: list[BuildJob] = []

    for _ in range(max_jobs):
        async with sessionmaker() as session:
            job_id_val = await session.scalar(_NEXT_QUEUED_BUILD_SQL)
        if job_id_val is None:
            break
        job_id = job_id_val if isinstance(job_id_val, uuid.UUID) else uuid.UUID(str(job_id_val))
        finished = await execute_build_job(
            job_id,
            sessionmaker=sessionmaker,
            store=store,
            settings=settings,
            source_fetcher=fetcher,
            build_runner=runner,
        )
        if finished is not None:
            completed.append(finished)

    return completed


async def _worker_loop(interval_s: float, backend: str, once: bool) -> int:
    settings = get_settings()
    sessionmaker = get_sessionmaker()
    store = create_object_store(settings)
    runner = create_build_runner(backend)
    fetcher = GitShallowSourceFetcher()
    while True:
        await run_once(
            sessionmaker,
            store,
            settings,
            source_fetcher=fetcher,
            build_runner=runner,
        )
        if once:
            return 0
        await asyncio.sleep(max(0.2, interval_s))


def main(argv: Sequence[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="FleetForge R10 server-side firmware builder")
    parser.add_argument("--once", action="store_true", help="Drain queued builds and exit")
    parser.add_argument("--interval", type=float, default=5.0, help="Poll interval in seconds")
    parser.add_argument(
        "--backend",
        choices=("auto", "docker", "local"),
        default="auto",
        help="Build runner backend",
    )
    args = parser.parse_args(argv)
    return asyncio.run(_worker_loop(args.interval, args.backend, args.once))


if __name__ == "__main__":
    raise SystemExit(main())

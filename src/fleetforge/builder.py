"""External container build worker CLI (`python -m fleetforge.builder`) (`R10-1`).

Polls `build_jobs` rows in state `queued` (`FOR UPDATE SKIP LOCKED`) and compiles
firmware in ephemeral single-tenant ESP-IDF containers outside the `fleetforge-api` pod.
"""

from __future__ import annotations

import argparse
import asyncio
import datetime as dt
import uuid
from collections.abc import Sequence

from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from fleetforge.builds import (
    DEFAULT_BUILD_LEASE_TTL_S,
    BuildJob,
    BuildRunner,
    GitShallowSourceFetcher,
    SourceFetcher,
    create_build_runner,
    execute_build_job,
)
from fleetforge.clock import now_utc
from fleetforge.config import Settings, get_settings
from fleetforge.db.base import get_sessionmaker
from fleetforge.storage.factory import create_object_store
from fleetforge.storage.objectstore import ObjectStore

_REAP_EXPIRED_LEASES_SQL = text(
    """
    UPDATE build_jobs
       SET status = 'queued',
           lease_owner = NULL,
           lease_expires_at = NULL
     WHERE status = 'running'
       AND lease_expires_at IS NOT NULL
       AND lease_expires_at <= :now
    RETURNING id
    """
)

_CLAIM_NEXT_QUEUED_BUILD_SQL = text(
    """
    UPDATE build_jobs
       SET status = 'running',
           started_at = COALESCE(started_at, :now),
           lease_owner = :worker_id,
           lease_expires_at = :lease_expires_at
     WHERE id = (
         SELECT id
           FROM build_jobs
          WHERE status = 'queued'
          ORDER BY created_at ASC, id ASC
          FOR UPDATE SKIP LOCKED
          LIMIT 1
     )
    RETURNING id
    """
)


async def reap_expired_build_leases(
    session: AsyncSession,
    *,
    now: dt.datetime | None = None,
) -> list[uuid.UUID]:
    """Return running build jobs whose worker lease expired back to `queued`."""
    current = now or now_utc()
    rows = (await session.scalars(_REAP_EXPIRED_LEASES_SQL, {"now": current})).all()
    return [val if isinstance(val, uuid.UUID) else uuid.UUID(str(val)) for val in rows]


async def run_once(
    sessionmaker: async_sessionmaker[AsyncSession],
    store: ObjectStore,
    settings: Settings,
    *,
    source_fetcher: SourceFetcher | None = None,
    build_runner: BuildRunner | None = None,
    max_jobs: int = 8,
    worker_id: str | None = None,
    lease_ttl_s: float = DEFAULT_BUILD_LEASE_TTL_S,
) -> list[BuildJob]:
    """Reap stale leases, atomically claim up to `max_jobs` queued builds, and execute them."""
    fetcher = source_fetcher or GitShallowSourceFetcher()
    runner = build_runner or create_build_runner()
    owner = worker_id or f"builder-{uuid.uuid4().hex[:12]}"
    completed: list[BuildJob] = []

    async with sessionmaker() as session:
        await reap_expired_build_leases(session)
        await session.commit()

    for _ in range(max_jobs):
        now = now_utc()
        lease_expires_at = now + dt.timedelta(seconds=max(1.0, lease_ttl_s))
        async with sessionmaker() as session:
            job_id_val = await session.scalar(
                _CLAIM_NEXT_QUEUED_BUILD_SQL,
                {
                    "now": now,
                    "worker_id": owner,
                    "lease_expires_at": lease_expires_at,
                },
            )
            await session.commit()
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
            claimed_worker_id=owner,
            lease_ttl_s=lease_ttl_s,
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
        choices=("auto", "docker"),
        default="auto",
        help="Build runner backend",
    )
    args = parser.parse_args(argv)
    return asyncio.run(_worker_loop(args.interval, args.backend, args.once))


if __name__ == "__main__":
    raise SystemExit(main())

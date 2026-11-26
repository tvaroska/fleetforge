"""External simulation worker CLI (`python -m fleetforge.sim_worker`) (`R9-1`).

Claims `artifacts` rows queued with `sim_status = 'pending'`, downloads their bytes from
`ObjectStore`, runs the configured `SimRunner` outside the `fleetforge-api` container, and
writes `(sim_status, sim_detail, sim_ran_at)` back to PostgreSQL.
"""

from __future__ import annotations

import argparse
import asyncio
import datetime as dt
import logging
import uuid
from collections.abc import Sequence

from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from fleetforge.clock import now_utc
from fleetforge.config import get_settings
from fleetforge.db.base import get_sessionmaker
from fleetforge.simulation import SimResult, SimRunner, SimStatus, create_sim_runner
from fleetforge.storage.blobs import blob_key
from fleetforge.storage.factory import create_object_store
from fleetforge.storage.objectstore import ObjectStore, ObjectStoreError

logger = logging.getLogger(__name__)

DEFAULT_SIM_LEASE_TTL_S = 180.0

_REAP_EXPIRED_SIM_LEASES_SQL = text(
    """
    UPDATE artifacts
       SET sim_status = 'pending',
           sim_lease_owner = NULL,
           sim_lease_expires_at = NULL
     WHERE sim_status = 'running'
       AND sim_lease_expires_at IS NOT NULL
       AND sim_lease_expires_at <= :now
    RETURNING sha256
    """
)

_CLAIM_NEXT_PENDING_SQL = text(
    """
    UPDATE artifacts
       SET sim_status = 'running',
           sim_lease_owner = :worker_id,
           sim_lease_expires_at = :lease_expires_at
     WHERE sha256 = (
         SELECT sha256
           FROM artifacts
          WHERE sim_status = 'pending'
          ORDER BY created_at ASC, sha256 ASC
          FOR UPDATE SKIP LOCKED
          LIMIT 1
     )
    RETURNING sha256, target, partition_layout, artifact_type
    """
)

_RECORD_SIM_RESULT_SQL = text(
    """
    UPDATE artifacts
       SET sim_status = :sim_status,
           sim_detail = :sim_detail,
           sim_runner = :sim_runner,
           sim_ran_at = :sim_ran_at,
           sim_lease_owner = NULL,
           sim_lease_expires_at = NULL
     WHERE sha256 = :sha256
       AND sim_status = 'running'
       AND sim_lease_owner = :worker_id
    RETURNING sha256
    """
)


async def reap_expired_sim_leases(
    session: AsyncSession,
    *,
    now: dt.datetime | None = None,
) -> list[str]:
    """Return running artifact simulations whose lease expired back to `pending`."""
    current = now or now_utc()
    rows = (await session.scalars(_REAP_EXPIRED_SIM_LEASES_SQL, {"now": current})).all()
    return [str(val) for val in rows]


async def run_once(
    sessionmaker: async_sessionmaker[AsyncSession],
    store: ObjectStore,
    runner: SimRunner,
    *,
    max_items: int = 16,
    worker_id: str | None = None,
    lease_ttl_s: float = DEFAULT_SIM_LEASE_TTL_S,
) -> list[tuple[str, SimResult]]:
    """Reap stale leases, claim up to `max_items` pending simulations, and persist verdicts."""
    owner = worker_id or f"sim-{uuid.uuid4().hex[:12]}"
    async with sessionmaker() as session:
        await reap_expired_sim_leases(session)
        await session.commit()

    processed: list[tuple[str, SimResult]] = []
    for _ in range(max_items):
        now = now_utc()
        lease_expires_at = now + dt.timedelta(seconds=max(1.0, lease_ttl_s))
        async with sessionmaker() as session:
            claimed = (
                await session.execute(
                    _CLAIM_NEXT_PENDING_SQL,
                    {"worker_id": owner, "lease_expires_at": lease_expires_at},
                )
            ).first()
            await session.commit()
        if claimed is None:
            break

        sha256 = str(claimed.sha256)
        target = str(claimed.target or "esp32")
        layout = str(claimed.partition_layout) if claimed.partition_layout else None
        artifact_type = str(claimed.artifact_type or "app")

        try:
            image = await store.get(blob_key(sha256))
            result = await runner.simulate(
                sha256=sha256,
                target=target,
                image=image,
                partition_layout=layout,
                artifact_type=artifact_type,
            )
        except ObjectStoreError as exc:
            logger.error("sim_worker could not read blob %s: %s", sha256, exc)
            result = SimResult(
                status=SimStatus.FAILED,
                detail="artifact bytes could not be fetched from object store",
                ran_at=now_utc(),
                runner="sim-worker",
            )

        async with sessionmaker() as session:
            updated = await session.scalar(
                _RECORD_SIM_RESULT_SQL,
                {
                    "sha256": sha256,
                    "worker_id": owner,
                    "sim_status": result.status.value,
                    "sim_detail": result.detail,
                    "sim_runner": result.runner,
                    "sim_ran_at": result.ran_at,
                },
            )
            await session.commit()
        if updated is None:
            logger.warning(
                "sim_worker %s lost lease on %s before recording %s; discarding stale verdict",
                owner,
                sha256[:12],
                result.status.value,
            )
            continue
        processed.append((sha256, result))
    return processed


async def _worker_loop(interval_s: float, backend: str, once: bool) -> int:
    settings = get_settings()
    sessionmaker = get_sessionmaker()
    store = create_object_store(settings)
    runner = create_sim_runner(backend)
    while True:
        await run_once(sessionmaker, store, runner)
        if once:
            return 0
        await asyncio.sleep(max(0.2, interval_s))


def main(argv: Sequence[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="FleetForge R9 pre-flight simulation worker")
    parser.add_argument("--once", action="store_true", help="Drain pending simulations and exit")
    parser.add_argument("--interval", type=float, default=5.0, help="Poll interval in seconds")
    parser.add_argument(
        "--backend",
        choices=("auto", "qemu"),
        default="auto",
        help="Simulation runner backend",
    )
    args = parser.parse_args(argv)
    return asyncio.run(_worker_loop(args.interval, args.backend, args.once))


if __name__ == "__main__":
    raise SystemExit(main())

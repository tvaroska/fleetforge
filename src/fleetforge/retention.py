"""How long device-reported debugging data lives, and the sweep that enforces it. R4-be-1.

`spec/prd.md` → *Retention*: telemetry samples **30 days**, device logs **7 days**. The
up/log proposal (`docs/features/health-telemetry.md` → *up/log proposal*, Patch B, owner
pending) adds **the newest 5 000 lines per device**, and R4-be-1 proposes **the newest
500 000 samples per device** for telemetry (*R4-be-1 as built*, a Patch for the same
table). Every number lives here, once; `protocol.py` derives its age limits from them and
`tests/test_device_health.py` reads the spec's row and compares.

**Plain tables, app-side retention** (DECISIONS 2026-10-15, R4-be-1). Dev and the test
suite run `postgres:16`, not TimescaleDB, so a hypertable or a Timescale retention policy
would make prod's schema differ from the one every test runs against. Two mechanisms,
each covering what the other cannot:

* **Bound on write** where it is cheap: `device_logs` (5 000 per device, `device_logs.py`)
  and `device_faults` (1 000 per device, `faults.py`) trim in the inserting transaction,
  the `progress.py` idiom.
* **This sweep** for everything that ages: a board that never writes again never trims,
  so its rows would outlive their window. The read helpers also filter on age, so expiry
  holds even when a sweep is late. The per-device telemetry cap lives here and not on
  write, because trimming 500 000 rows on every five-second batch costs far more than an
  hourly pass (between two sweeps one board can add at most ~11 000 rows).

The sweep runs in the ingestor only (`ingestor/main.py::retention_loop`): it is a single
instance by design, so two sweepers never race; the api runs N workers.

Transport-agnostic like `progress.py`: no settings, no clock. The caller passes `now`
and commits.
"""

import datetime as dt
import logging
from dataclasses import dataclass
from typing import TYPE_CHECKING

from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncSession

from fleetforge.storage.blobs import blob_key
from fleetforge.storage.objectstore import ObjectStoreError

if TYPE_CHECKING:
    from fleetforge.storage.objectstore import ObjectStore

logger = logging.getLogger("fleetforge.retention")

# `up/log`: 7 days (spec/prd.md -> Retention) and the newest 5 000 lines per device (the
# up/log proposal's Patch B).
LOG_RETENTION = dt.timedelta(days=7)
LOG_MAX_LINES_PER_DEVICE = 5_000

# `up/telemetry`: 30 days (spec/prd.md -> Retention) and, proposed by R4-be-1, the newest
# 500 000 samples per device. The firmware's own limits allow ~3 samples/s sustained,
# about 7.8 M rows per device in 30 days, on a shared VM.
TELEMETRY_RETENTION = dt.timedelta(days=30)
TELEMETRY_MAX_SAMPLES_PER_DEVICE = 500_000

# `device_faults`: the fault window the API counts over is a day (`fault_window_s`); a
# month of rows is kept so the window can widen without losing history. The row cap is
# a crash-looping board's bound: one row per boot, a boot every ~10 s.
FAULT_RETENTION = dt.timedelta(days=30)
FAULT_MAX_ROWS_PER_DEVICE = 1_000

# `spec/prd.md` -> *Retention*: Revoked/expired tokens are kept 90 days, then purged.
# Used enrollment tokens are kept (`used_by_device_id` is enrollment provenance).
TOKEN_RETENTION = dt.timedelta(days=90)

# `spec/prd.md` -> *Capacity*: Stored versions per platform: 20, oldest pruned; the
# running and previous version are never pruned.
MAX_VERSIONS_PER_PLATFORM = 20

# How often the ingestor sweeps. Hourly: the read predicates already hide expired rows,
# so the sweep only bounds storage.
SWEEP_INTERVAL_S = 3600.0

_DELETE_LOGS_SQL = text("DELETE FROM device_logs WHERE logged_at <= :cutoff")
_DELETE_TELEMETRY_SQL = text("DELETE FROM device_telemetry WHERE sample_at <= :cutoff")
_DELETE_METRICS_SQL = text("DELETE FROM device_metrics WHERE sample_at <= :cutoff")
_DELETE_FAULTS_SQL = text("DELETE FROM device_faults WHERE at <= :cutoff")

_DELETE_ENROLLMENT_TOKENS_SQL = text(
    """
    DELETE FROM enrollment_tokens
     WHERE used_at IS NULL
       AND (
           (revoked_at IS NOT NULL AND revoked_at <= :cutoff)
           OR (revoked_at IS NULL AND expires_at <= :cutoff)
       )
    """
)

_DELETE_ADMIN_TOKENS_SQL = text(
    """
    DELETE FROM admin_tokens
     WHERE (revoked_at IS NOT NULL AND revoked_at <= :cutoff)
        OR (expires_at IS NOT NULL AND expires_at <= :cutoff)
    """
)

# Keep the newest `:version_cap` labels per `(project_id, target)`, plus any version
# that is a live device's running `fw_version` or the `from_version` / `artifact_version`
# of a live device's latest or latest-confirmed deploy transaction.
_PRUNE_ARTIFACT_VERSIONS_SQL = text(
    """
    WITH ranked AS (
        SELECT project_id,
               target,
               version,
               row_number() OVER (
                   PARTITION BY project_id, target
                   ORDER BY created_at DESC, version DESC
               ) AS rn
          FROM artifact_versions
    ),
    latest_deploy AS (
        SELECT DISTINCT ON (e.device_id)
               d.project_id,
               d.platform_type AS target,
               e.from_version,
               e.artifact_version
          FROM deploy_events e
          JOIN devices d ON d.device_id = e.device_id
         WHERE d.decommissioned_at IS NULL
         ORDER BY e.device_id, e.at DESC, e.id DESC
    ),
    latest_confirmed AS (
        SELECT DISTINCT ON (e.device_id)
               d.project_id,
               d.platform_type AS target,
               e.from_version,
               e.artifact_version
          FROM deploy_events e
          JOIN devices d ON d.device_id = e.device_id
         WHERE d.decommissioned_at IS NULL
           AND e.state = 'confirmed'
         ORDER BY e.device_id, e.at DESC, e.id DESC
    ),
    protected AS (
        SELECT project_id, platform_type AS target, fw_version AS version
          FROM devices
         WHERE decommissioned_at IS NULL
           AND fw_version IS NOT NULL
        UNION
        SELECT project_id, target, from_version AS version
          FROM latest_deploy
         WHERE from_version IS NOT NULL
        UNION
        SELECT project_id, target, artifact_version AS version
          FROM latest_deploy
         WHERE artifact_version IS NOT NULL
        UNION
        SELECT project_id, target, from_version AS version
          FROM latest_confirmed
         WHERE from_version IS NOT NULL
        UNION
        SELECT project_id, target, artifact_version AS version
          FROM latest_confirmed
         WHERE artifact_version IS NOT NULL
    )
    DELETE FROM artifact_versions av
     USING ranked r
     WHERE av.project_id = r.project_id
       AND av.target = r.target
       AND av.version = r.version
       AND r.rn > :version_cap
       AND NOT EXISTS (
           SELECT 1
             FROM protected p
            WHERE p.project_id = av.project_id
              AND p.target = av.target
              AND p.version = av.version
       )
    """
)

# Garbage-collect user_firmware artifacts (and their backing object-store blobs)
# once no surviving artifact_versions row or builds.outputs entry references them.
# Never touches agent_* bundle rows.
_PRUNE_ORPHAN_ARTIFACTS_SQL = text(
    """
    DELETE FROM artifacts a
     WHERE a.kind = 'user_firmware'
       AND NOT EXISTS (
           SELECT 1
             FROM artifact_versions av
            WHERE av.sha256 = a.sha256
       )
       AND NOT EXISTS (
           SELECT 1
             FROM builds b,
                  jsonb_each(b.outputs) AS kv
            WHERE kv.value ->> 'sha256' = a.sha256
       )
       AND NOT EXISTS (
           SELECT 1
             FROM deploy_events req
            WHERE req.state = 'requested'
              AND req.cmd_id IS NOT NULL
              AND req.detail ->> 'sha256' = a.sha256
              AND NOT EXISTS (
                  SELECT 1
                    FROM deploy_events term
                   WHERE term.device_id = req.device_id
                     AND term.cmd_id = req.cmd_id
                     AND term.is_terminal
              )
       )
    RETURNING a.sha256
    """
)

# Devices holding more than `:cap` samples. Uses `ix_device_telemetry_device_id_name_sample_at`
# to probe only devices that actually have a `:cap`-th row per metric or overall.
_OVER_CAP_SQL = text(
    """
    SELECT d.device_id
      FROM devices d
     WHERE EXISTS (
         SELECT 1
           FROM device_telemetry t
          WHERE t.device_id = d.device_id
         OFFSET :cap
          LIMIT 1
     )
    """
)

# Keep this device's newest `:cap` samples. The subquery names the newest row that does
# NOT survive (`OFFSET :cap`); everything at or below it in `(sample_at, id)` order goes.
# `id` breaks ties: a batch can carry two samples with the same `age_ms`.
_CAP_TELEMETRY_SQL = text(
    """
    DELETE FROM device_telemetry
     WHERE device_id = :device_id
       AND (sample_at, id) <= (
           SELECT sample_at, id
             FROM device_telemetry
            WHERE device_id = :device_id
            ORDER BY sample_at DESC, id DESC
           OFFSET :cap
            LIMIT 1
       )
    """
)


@dataclass(frozen=True, slots=True)
class SweepResult:
    """Rows one sweep deleted, per reason. Counts only: never a row's content."""

    logs: int = 0
    telemetry_expired: int = 0
    telemetry_capped: int = 0
    metrics: int = 0
    faults: int = 0
    tokens: int = 0
    artifact_versions: int = 0
    artifacts: int = 0
    orphan_sha256s: tuple[str, ...] = ()

    @property
    def total(self) -> int:
        return (
            self.logs
            + self.telemetry_expired
            + self.telemetry_capped
            + self.metrics
            + self.faults
            + self.tokens
            + self.artifact_versions
            + self.artifacts
        )


def _rowcount(result: object) -> int:
    """`CursorResult.rowcount`, typed loosely: `session.execute` returns `Result`."""
    count = getattr(result, "rowcount", 0)
    return count if isinstance(count, int) and count > 0 else 0


async def sweep(
    session: AsyncSession,
    *,
    now: dt.datetime,
    telemetry_cap: int = TELEMETRY_MAX_SAMPLES_PER_DEVICE,
    version_cap: int = MAX_VERSIONS_PER_PLATFORM,
    store: "ObjectStore | None" = None,
    prune_orphans: bool = False,
) -> SweepResult:
    """Delete everything past its window, then cap each device's telemetry and platform versions.

    The cutoffs are `<=` so a sweep removes exactly what the read predicates
    (`logged_at > now - 7 d`, `sample_at > now - 30 d`) already hide.
    """
    logs = _rowcount(await session.execute(_DELETE_LOGS_SQL, {"cutoff": now - LOG_RETENTION}))
    telemetry_cutoff = now - TELEMETRY_RETENTION
    telemetry_expired = _rowcount(
        await session.execute(_DELETE_TELEMETRY_SQL, {"cutoff": telemetry_cutoff})
    )
    over_cap = (await session.scalars(_OVER_CAP_SQL, {"cap": telemetry_cap})).all()
    telemetry_capped = 0
    for device_id in over_cap:
        telemetry_capped += _rowcount(
            await session.execute(
                _CAP_TELEMETRY_SQL, {"device_id": device_id, "cap": telemetry_cap}
            )
        )
    metrics = _rowcount(await session.execute(_DELETE_METRICS_SQL, {"cutoff": telemetry_cutoff}))
    faults = _rowcount(await session.execute(_DELETE_FAULTS_SQL, {"cutoff": now - FAULT_RETENTION}))
    token_cutoff = now - TOKEN_RETENTION
    tokens = _rowcount(
        await session.execute(_DELETE_ENROLLMENT_TOKENS_SQL, {"cutoff": token_cutoff})
    ) + _rowcount(await session.execute(_DELETE_ADMIN_TOKENS_SQL, {"cutoff": token_cutoff}))
    artifact_versions = _rowcount(
        await session.execute(_PRUNE_ARTIFACT_VERSIONS_SQL, {"version_cap": version_cap})
    )
    orphan_sha256s: list[str] = []
    if artifact_versions > 0 or store is not None or prune_orphans:
        orphan_sha256s = [
            str(sha)
            for sha in (await session.scalars(_PRUNE_ORPHAN_ARTIFACTS_SQL)).all()
        ]
    if store is not None and orphan_sha256s:
        for sha256 in orphan_sha256s:
            try:
                await store.delete(blob_key(sha256))
            except ObjectStoreError as exc:
                logger.warning("retention blob delete failed for %s: %s", sha256, exc)
    return SweepResult(
        logs=logs,
        telemetry_expired=telemetry_expired,
        telemetry_capped=telemetry_capped,
        metrics=metrics,
        faults=faults,
        tokens=tokens,
        artifact_versions=artifact_versions,
        artifacts=len(orphan_sha256s),
        orphan_sha256s=tuple(orphan_sha256s),
    )

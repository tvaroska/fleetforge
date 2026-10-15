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
from dataclasses import dataclass

from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncSession

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

# How often the ingestor sweeps. Hourly: the read predicates already hide expired rows,
# so the sweep only bounds storage.
SWEEP_INTERVAL_S = 3600.0

_DELETE_LOGS_SQL = text("DELETE FROM device_logs WHERE logged_at <= :cutoff")
_DELETE_TELEMETRY_SQL = text("DELETE FROM device_telemetry WHERE sample_at <= :cutoff")
_DELETE_METRICS_SQL = text("DELETE FROM device_metrics WHERE sample_at <= :cutoff")
_DELETE_FAULTS_SQL = text("DELETE FROM device_faults WHERE at <= :cutoff")

# Devices holding more than `:cap` samples. A full pass over the series index, hourly.
_OVER_CAP_SQL = text(
    """
    SELECT device_id
      FROM device_telemetry
     GROUP BY device_id
    HAVING count(*) > :cap
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

    @property
    def total(self) -> int:
        return (
            self.logs + self.telemetry_expired + self.telemetry_capped + self.metrics + self.faults
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
) -> SweepResult:
    """Delete everything past its window, then cap each device's telemetry. Caller commits.

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
    return SweepResult(
        logs=logs,
        telemetry_expired=telemetry_expired,
        telemetry_capped=telemetry_capped,
        metrics=metrics,
        faults=faults,
    )

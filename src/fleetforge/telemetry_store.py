"""User-defined telemetry (`up/telemetry`) — the record and the reads. R4-be-1.

Named `telemetry_store`, not `telemetry`, because `fleetforge.simulator.telemetry` (the
firmware ring in Python) already has that name.

Two tables, one owner:

* `device_telemetry` — every sample, `sample_at = received_at - age_ms`
  (`clock.device_time`; the device's clock is never read). Retention is 30 days and the
  newest 500 000 samples per device, both enforced by the sweep (`fleetforge.retention`)
  rather than on write, plus an age predicate on every read.
* `device_metrics` — the latest value per (device, metric), upserted with each batch and
  only ever forwards in `sample_at`, so a backlog flushed after a reconnect cannot
  overwrite a newer value. It exists because `GET /v1/devices` is re-read on every SSE
  hint and a `DISTINCT ON (device_id, name)` over the samples would walk all of them.

Names are validated (`TELEMETRY_NAME_RE`) and values are finite doubles before anything
reaches here, so nothing stored is free text. Transport-agnostic: the caller passes `now`
and owns the transaction; samples are taken structurally (`Sample`), so the API can read
through this module without importing `fleetforge.ingestor`.
"""

import datetime as dt
import re
from collections.abc import Sequence
from dataclasses import dataclass
from typing import Protocol

from sqlalchemy import insert, text
from sqlalchemy.dialects.postgresql import insert as pg_insert
from sqlalchemy.ext.asyncio import AsyncSession

from fleetforge.clock import device_time
from fleetforge.db.models import DeviceMetric, DeviceTelemetry
from fleetforge.retention import TELEMETRY_RETENTION

# A valid Prometheus metric name; the firmware's FF_TELEMETRY_NAME_MAX (31) is `{0,30}` + 1
# (tests/test_telemetry.py holds the firmware, the simulator and this together). Here and
# not in `ingestor/protocol.py` (which imports it) because the API validates the series
# read's `?name=` with it and may not import `fleetforge.ingestor`.
TELEMETRY_NAME_RE = re.compile(r"[a-z][a-z0-9_]{0,30}")

# The most metrics per device `GET /v1/devices` carries: the most recently updated ones,
# returned sorted by name. A firmware with more still stores them all.
MAX_METRICS_PER_DEVICE = 32

# The newest `:per_device` metrics of each device, inside the window, sorted by name.
LATEST_METRICS_SQL = text(
    """
    SELECT device_id, name, value, sample_at
      FROM (
          SELECT device_id, name, value, sample_at,
                 row_number() OVER (
                     PARTITION BY device_id ORDER BY sample_at DESC, name
                 ) AS rn
            FROM device_metrics
           WHERE device_id = ANY(:device_ids)
             AND sample_at > :since
      ) AS ranked
     WHERE rn <= :per_device
     ORDER BY device_id, name
    """
)

SERIES_SQL = text(
    """
    SELECT sample_at, value
      FROM device_telemetry
     WHERE device_id = :device_id
       AND name = :name
       AND sample_at > :since
     ORDER BY sample_at DESC, id DESC
     LIMIT :limit
    """
)


class Sample(Protocol):
    """One decoded sample (`ingestor.protocol.TelemetrySample`), structurally."""

    @property
    def name(self) -> str: ...
    @property
    def value(self) -> float: ...
    @property
    def age_ms(self) -> int: ...


@dataclass(frozen=True, slots=True)
class MetricSnapshot:
    """The latest value of one metric."""

    name: str
    value: float
    at: dt.datetime


@dataclass(frozen=True, slots=True)
class SeriesPoint:
    at: dt.datetime
    value: float


async def record_samples(
    session: AsyncSession,
    *,
    device_id: str,
    samples: Sequence[Sample],
    received_at: dt.datetime,
) -> int:
    """Store a batch and move each metric's latest value forward; return samples stored."""
    if not samples:
        return 0
    stamped = [(sample, device_time(received_at, sample.age_ms)) for sample in samples]
    await session.execute(
        insert(DeviceTelemetry),
        [
            {"device_id": device_id, "name": sample.name, "value": sample.value, "sample_at": at}
            for sample, at in stamped
        ],
    )

    # The newest sample per name in this batch; on a tie the later one in the batch wins
    # (the batch is oldest first).
    latest: dict[str, tuple[float, dt.datetime]] = {}
    for sample, at in stamped:
        current = latest.get(sample.name)
        if current is None or at >= current[1]:
            latest[sample.name] = (sample.value, at)
    values = [
        {"device_id": device_id, "name": name, "value": value, "sample_at": at}
        for name, (value, at) in latest.items()
    ]
    stmt = pg_insert(DeviceMetric).values(values)
    stmt = stmt.on_conflict_do_update(
        index_elements=[DeviceMetric.device_id, DeviceMetric.name],
        set_={"value": stmt.excluded.value, "sample_at": stmt.excluded.sample_at},
        where=stmt.excluded.sample_at >= DeviceMetric.sample_at,
    )
    await session.execute(stmt)
    return len(stamped)


async def latest_metrics(
    session: AsyncSession,
    *,
    device_ids: Sequence[str],
    now: dt.datetime,
    per_device: int = MAX_METRICS_PER_DEVICE,
) -> dict[str, list[MetricSnapshot]]:
    """Each device's latest metric values inside the 30-day window, sorted by name.

    `device_ids` comes from the caller's own list (the `deploys.latest_deploys` rule).
    Devices with no metric are absent.
    """
    rows = (
        await session.execute(
            LATEST_METRICS_SQL,
            {
                "device_ids": list(device_ids),
                "since": now - TELEMETRY_RETENTION,
                "per_device": per_device,
            },
        )
    ).all()
    metrics: dict[str, list[MetricSnapshot]] = {}
    for row in rows:
        metrics.setdefault(row.device_id, []).append(
            MetricSnapshot(name=row.name, value=row.value, at=row.sample_at)
        )
    return metrics


async def read_series(
    session: AsyncSession, *, device_id: str, name: str, now: dt.datetime, limit: int
) -> list[SeriesPoint]:
    """One metric's newest `limit` samples inside the 30-day window, newest first."""
    rows = (
        await session.execute(
            SERIES_SQL,
            {
                "device_id": device_id,
                "name": name,
                "since": now - TELEMETRY_RETENTION,
                "limit": limit,
            },
        )
    ).all()
    return [SeriesPoint(at=row.sample_at, value=row.value) for row in rows]

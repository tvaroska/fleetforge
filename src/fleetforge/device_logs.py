"""Firmware log lines (`up/log`) — the record and the read. R4-be-1.

The server half of the up/log proposal (`docs/features/health-telemetry.md` -> *up/log
proposal*; Patch A, the payload, and Patch B, the 5 000-line bound, are the owner's). The
TODO line scoped the table and the bound into R4-be-1, so the server is built ahead of the
spec, exactly as R4-fw-2 built `up/telemetry` ahead of its Patch A.

Rules this module owns:

1. **Bounded on write** (the `progress.py` idiom). `record_log_batch` inserts the batch
   and trims the device to the newest `max_lines` in the same transaction, so a looping
   board cannot grow the table between sweeps. Age (7 days) is the sweep's job
   (`fleetforge.retention`) and every read's predicate, so a silent board's lines expire
   too.
2. **`text` is untrusted.** Stored through `deploys.sanitise_device_text` (no control
   characters, URLs redacted, 200 characters), returned only to the admin API, and never
   written into a server log by anyone.
3. **Gaps are rows.** `dropped` on a line is the number of lines lost immediately before
   it: the device's own `dropped` plus the lines the server could not use, stored on the
   batch's first stored line. A batch with a gap and no usable line stores one marker
   (`text IS NULL`) at its receipt time; a batch with neither stores nothing.

Transport-agnostic (no FastAPI, no settings, no clock): the caller passes `now`, the cap
and owns the transaction. It takes the decoded payload structurally (`LogBatch`) rather
than importing `fleetforge.ingestor`, because the API reads through this module too.
"""

import datetime as dt
from collections.abc import Sequence
from dataclasses import dataclass
from typing import Any, Protocol

from sqlalchemy import insert, text
from sqlalchemy.ext.asyncio import AsyncSession

from fleetforge.clock import device_time
from fleetforge.db.models import DeviceLog
from fleetforge.deploys import sanitise_device_text
from fleetforge.retention import LOG_MAX_LINES_PER_DEVICE, LOG_RETENTION

# `device_logs.dropped` is INTEGER; a hostile count must not turn into a DataError that
# loses the batch.
MAX_DROPPED = 2**31 - 1
GAP_MARKER_LEVEL = "I"

# Keep this device's newest `:keep` lines. The subquery names the newest line that does
# NOT survive (`OFFSET :keep`); everything at or below it in `(logged_at, id)` order goes,
# read straight off `ix_device_logs_device_id_logged_at`. Fewer lines than the cap make
# it NULL, and nothing is deleted. `id` breaks ties: lines of one batch can share an age.
TRIM_SQL = text(
    """
    DELETE FROM device_logs
     WHERE device_id = :device_id
       AND (logged_at, id) <= (
           SELECT logged_at, id
             FROM device_logs
            WHERE device_id = :device_id
            ORDER BY logged_at DESC, id DESC
           OFFSET :keep
            LIMIT 1
       )
    """
)

TAIL_SQL = text(
    """
    SELECT id, logged_at, received_at, level, text, dropped
      FROM device_logs
     WHERE device_id = :device_id
       AND logged_at > :since
     ORDER BY logged_at DESC, id DESC
     LIMIT :limit
    """
)


class LogLineIn(Protocol):
    """One decoded line (`ingestor.protocol.LogLine`), structurally."""

    @property
    def text(self) -> str: ...
    @property
    def level(self) -> str: ...
    @property
    def age_ms(self) -> int: ...


class LogBatch(Protocol):
    """One decoded payload (`ingestor.protocol.LogPayload`), structurally."""

    @property
    def dropped(self) -> int | None: ...
    @property
    def lines(self) -> Sequence[LogLineIn]: ...
    @property
    def unusable(self) -> int: ...


@dataclass(frozen=True, slots=True)
class StoredLogLine:
    """One line as the API serves it. `text is None` is a gap marker."""

    id: int
    logged_at: dt.datetime
    received_at: dt.datetime
    level: str
    text: str | None
    dropped: int


def _rows(device_id: str, batch: LogBatch, received_at: dt.datetime) -> list[dict[str, Any]]:
    gap = min((batch.dropped or 0) + batch.unusable, MAX_DROPPED)
    if not batch.lines:
        if not gap:
            return []
        return [
            {
                "device_id": device_id,
                "logged_at": received_at,
                "received_at": received_at,
                "level": GAP_MARKER_LEVEL,
                "text": None,
                "dropped": gap,
            }
        ]
    return [
        {
            "device_id": device_id,
            "logged_at": device_time(received_at, line.age_ms),
            "received_at": received_at,
            "level": line.level,
            "text": sanitise_device_text(line.text),
            "dropped": gap if index == 0 else 0,
        }
        for index, line in enumerate(batch.lines)
    ]


async def record_log_batch(
    session: AsyncSession,
    *,
    device_id: str,
    payload: LogBatch,
    received_at: dt.datetime,
    max_lines: int = LOG_MAX_LINES_PER_DEVICE,
) -> int:
    """Store one batch (oldest first, so ids ascend with time) and trim; return rows stored."""
    rows = _rows(device_id, payload, received_at)
    if not rows:
        return 0
    await session.execute(insert(DeviceLog), rows)
    await session.execute(TRIM_SQL, {"device_id": device_id, "keep": max_lines})
    return len(rows)


async def read_tail(
    session: AsyncSession, *, device_id: str, now: dt.datetime, limit: int
) -> list[StoredLogLine]:
    """The newest `limit` lines inside the 7-day window, newest first.

    The age predicate is what keeps the window true when a sweep is late or a board has
    gone silent; the trim only bounds the count.
    """
    rows = (
        await session.execute(
            TAIL_SQL, {"device_id": device_id, "since": now - LOG_RETENTION, "limit": limit}
        )
    ).all()
    return [
        StoredLogLine(
            id=row.id,
            logged_at=row.logged_at,
            received_at=row.received_at,
            level=row.level,
            text=row.text,
            dropped=row.dropped,
        )
        for row in rows
    ]

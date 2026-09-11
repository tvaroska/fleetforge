"""Boot/enrol progress — the record and the read. S0-fw-1.

The gap this closes: a board is invisible between "flashed" and "online". `agent_main.c`
walks link → SNTP → HTTPS enroll → MQTT and says so **only on its serial console**, so
the dashboard has nothing to show until the first retained `up/announce` reaches the
ingestor — and that window is exactly where a first-hardware attempt fails.

**The honest limit, stated so nobody designs around it:** a board with no route to the
server cannot report anything, and no amount of server-side cleverness changes that.
This earns its keep for the boards that get *partway* — link up but enroll refused,
enrolled but the broker refuses the credential — for sleepy boards, and for
re-enrolment after a rotation. A board that never reaches the network is still a
serial-console problem (`docs/features/enrollment.md`).

Three rules this module owns, and the reason each lives here rather than in the router:

1. **The table is bounded on write.** A board stuck retrying enrolment every 60 s
   reports forever. Unlike `deploy_events` — KPI history, kept forever — these rows
   are a debugging aid, so `record_progress` trims to the newest
   `max_rows_per_device` for that device in the same transaction as the insert. There
   is no sweeper job to forget to run, and no unbounded table if one is forgotten.
2. **`stalled` is derived on read, never stored** — from the age of the newest row,
   in this one function, exactly as `presence.is_online` derives `online`. A stored
   `stalled` would be wrong the moment nothing wrote to it.
3. **What counts as an arrival that already finished** is `has_already_arrived`, and
   nothing else may re-decide it. It is the difference between a board that is
   arriving and one that arrived and later died, and the router needs it to keep the
   second out of the arriving list (S0-fe-3).

Transport-agnostic like `fleetforge.presence` and `fleetforge.registry`: no FastAPI
import, no settings object, no clock — the caller passes `now`, the window and the cap.
"""

import datetime as dt
import logging
import uuid
from dataclasses import dataclass

from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncSession

from fleetforge.db.models import Device, DeviceProgress

logger = logging.getLogger(__name__)

# Keep the newest `:keep` rows for this device, drop the rest. `id` (an identity
# column) and not `at` is the tiebreaker, because several stages of one boot can land
# inside the same clock tick and `at DESC` alone would make the survivor arbitrary.
TRIM_SQL = text(
    """
    DELETE FROM device_progress
     WHERE device_id = :device_id
       AND id NOT IN (
           SELECT id FROM device_progress
            WHERE device_id = :device_id
            ORDER BY at DESC, id DESC
            LIMIT :keep
       )
    """
)

# The newest row per device inside the window. `DISTINCT ON` is the reason this is
# hand-written SQL: it reads straight down `ix_device_progress_device_id_at` instead
# of sorting a window function's output, and at 25 devices the clarity is worth more
# than the speed anyway.
LATEST_SQL = text(
    """
    SELECT DISTINCT ON (device_id) device_id, at, stage, detail
      FROM device_progress
     WHERE at > now() - make_interval(secs => :window_s)
     ORDER BY device_id, at DESC, id DESC
    """
)


@dataclass(frozen=True, slots=True)
class Arrival:
    """One board's newest reported stage, with staleness already decided.

    A plain dataclass rather than an ORM row: the caller wants the derived answer, and
    handing back a `DeviceProgress` would invite a second place to compute `stalled`.
    """

    device_id: str
    stage: str
    detail: str | None
    at: dt.datetime
    stalled: bool


async def record_progress(
    session: AsyncSession,
    *,
    device_id: str,
    token_id: uuid.UUID,
    stage: str,
    detail: str | None,
    max_rows_per_device: int,
) -> DeviceProgress:
    """Append one stage report and trim this device's history to the cap.

    Insert and trim share the caller's transaction, so a rolled-back request neither
    records a stage nor destroys older ones. `at` is left to the column default —
    server receipt time — because a board that has not run SNTP yet has no clock worth
    reading (`spec/device-protocol.md` → *Clock*).
    """
    row = DeviceProgress(device_id=device_id, token_id=token_id, stage=stage, detail=detail)
    session.add(row)
    # Before the trim, so the row this call just added is one of the survivors.
    await session.flush()
    await session.execute(TRIM_SQL, {"device_id": device_id, "keep": max_rows_per_device})
    return row


async def latest_progress(
    session: AsyncSession,
    *,
    now: dt.datetime,
    window_s: int,
    stall_s: int,
) -> list[Arrival]:
    """The newest stage each board reported inside `window_s`, newest board first.

    `stalled` means the board's newest stage is older than `stall_s` — it said
    something and then stopped, which is the signal the dashboard exists to show. It
    is not a judgement about whether the board is *broken*: a slow SNTP or a long
    enroll retry reads the same, and the operator reads the stage to tell them apart.
    """
    rows = (await session.execute(LATEST_SQL, {"window_s": window_s})).all()
    cutoff = dt.timedelta(seconds=stall_s)
    arrivals = [
        Arrival(
            device_id=row.device_id,
            stage=row.stage,
            detail=row.detail,
            at=row.at,
            stalled=(now - row.at) > cutoff,
        )
        for row in rows
    ]
    arrivals.sort(key=lambda arrival: (arrival.at, arrival.device_id), reverse=True)
    return arrivals


def has_already_arrived(device: Device | None, *, stage_at: dt.datetime) -> bool:
    """Whether `stage_at` belongs to an arrival this board already completed. S0-fe-3.

    A board that reached `mqtt_connected` and then lost power used to come back into
    the arriving list the moment presence decayed — labelled "stalled at
    `mqtt_connected`", duplicating a row the fleet list was already showing as
    offline. "Not currently online" cannot tell that board apart from one that is
    still on its way up, because both are offline with a recent stage. This can: the
    board that already arrived has a broker credential and a `last_seen` *newer* than
    anything it reported.

    Each condition is load-bearing:

    * **a row at all** — a board mid-arrival has no `devices` row yet, which is the
      whole point of the feature.
    * **`broker_provisioned_at`** — the last step of the arrival sequence, set by
      `/v1/enroll` once the dynsec credential lands. (`enrolled_at` is in the same
      family but is `NOT NULL` with a default, so testing it decides nothing.) Without
      this, a board that enrolled and was then refused by the broker would be hidden —
      the case that earns the feature.
    * **`last_seen`** — the board actually spoke to the broker at least once. An
      enrolled board that never connected has none, and must stay visible.
    * **`stage_at <= last_seen`** — and this is what keeps a *re-flashed* board
      visible. Re-enrolment deliberately leaves `last_seen` untouched
      (`registry.py`) and the ingestor only ever advances it (`GREATEST`, see
      `ingestor/store.py`), so a board reporting stages again after a re-flash reports
      them strictly newer than its stale `last_seen` and reappears here for free.

    Accepted limit: a board that reports `mqtt_connected` and dies before any live
    message advances `last_seen` past that report still reads as arriving. It never
    completed a heartbeat, so that is honest rather than wrong.
    """
    if device is None:
        return False
    if device.broker_provisioned_at is None or device.last_seen is None:
        return False
    return stage_at <= device.last_seen

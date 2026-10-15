"""Fault resets — the record, the reconnect dedupe and the count. R4-be-1.

The fleet view shows "this board panicked twice today". That number is **derived on
read** from `device_faults` (one row per boot whose `reset_reason` is a fault), never a
stored counter: the `stalled` / `is_online` discipline, because a counter is wrong the
moment nothing decrements it.

**The reconnect over-count, and why the heartbeat fixes it.** The agent republishes
`up/announce` on **every broker connect**, so a Wi-Fi blip inside one boot repeats the
boot's `reset_reason` (S0-bug-3: prod board `84c7bbe0e8c0` reconnected at 22:49:26 with no
reboot). Counting every live `panic` announce would show "3 panics" for one panic and two
blips, and the announce carries nothing that tells a boot from a reconnect. The heartbeat
does: `uptime_s`. So:

1. `record_fault`: a **live** announce of a registered device whose reason is in
   `FAULT_REASONS` inserts a row at the announce's receipt time. A retained replay never
   does (it repeats an old boot on every ingestor reconnect).
2. `forget_reconnects`: a **live** heartbeat with a usable `uptime_s` dates the current
   boot, `boot_floor = received_at - uptime_s - 1 s`. Every row of the device at or after
   the floor belongs to this boot; the earliest is kept and the rest, reconnect
   announces, are deleted.

**Why the floor is where it is.** Every timestamp is a server receipt time. Delivery
latency and esp-mqtt's outbox queueing can only make `received_at - uptime_s` **later**
than the true start of the boot, never earlier. `uptime_s` is whole seconds, truncated,
which also errs later, by up to 1 s: without a correction the boot's own first announce,
when it is sent within a second of the uptime origin, falls below the floor and the
reconnect row is kept as "the earliest" (found on the dev stack with the simulator, whose
announce leaves ~0.1 s after its uptime origin; an ethernet board can be that quick too).
So the floor is moved 1 s earlier, `UPTIME_RESOLUTION`, and is then at most 1 s before the
true boot start. An earlier boot's announce therefore falls inside the window only if it
was received less than 1 s before the current boot started, that is, a boot that died and
went through reset and the bootloader within about a second of announcing. Even then it
is kept (it is the earliest row) and the current boot's announce is the one deleted, so
the count is low by exactly one, never zero for a fault that happened. The cases:

* Crash loop (boots that die before any heartbeat): no heartbeat, nothing deleted, every
  announce counted, which is right, because every one was a boot.
* A queued heartbeat whose floor lands after the boot's first announce: the earliest
  included row may be a reconnect, so one extra row survives until the next fresh
  heartbeat, which sees both and deletes the reconnect. A QoS 1 redelivered announce is
  deduped the same way.
* **Named gaps:** reconnects inside a boot that never heartbeats are counted (rare: a boot
  shorter than one `hb_s` that also reconnected); a fault boot that died within about a
  second of announcing, followed by a boot that heartbeats, is counted as one (above).

This module owns `device_faults` for reads and writes (the `progress.py` /
`deploys.py` idiom). Transport-agnostic: the caller passes times and caps and owns the
transaction; the ingestor wraps each write in a SAVEPOINT so it never costs the message.
"""

import datetime as dt
from collections.abc import Sequence

from sqlalchemy import insert, text
from sqlalchemy.ext.asyncio import AsyncSession

from fleetforge.db.models import DeviceFault
from fleetforge.retention import FAULT_MAX_ROWS_PER_DEVICE

# `uptime_s` is whole seconds, truncated: the boot started up to this much before
# `received_at - uptime_s` (module docstring).
UPTIME_RESOLUTION = dt.timedelta(seconds=1)

# The reset reasons the fleet view counts. One tuple, one module: extending it is
# additive, and history for a new reason starts at the change.
FAULT_REASONS = ("panic", "task_wdt", "brownout")

# Keep this device's newest `:keep` rows. The subquery names the newest row that does
# NOT survive; everything at or below it in `(at, id)` order goes. NULL (fewer rows than
# the cap) deletes nothing.
TRIM_SQL = text(
    """
    DELETE FROM device_faults
     WHERE device_id = :device_id
       AND (at, id) <= (
           SELECT at, id
             FROM device_faults
            WHERE device_id = :device_id
            ORDER BY at DESC, id DESC
           OFFSET :keep
            LIMIT 1
       )
    """
)

# Every row of the current boot but its earliest. No row at or after the floor makes the
# subquery NULL, and `id <> NULL` deletes nothing.
FORGET_RECONNECTS_SQL = text(
    """
    DELETE FROM device_faults
     WHERE device_id = :device_id
       AND at >= :boot_floor
       AND id <> (
           SELECT id
             FROM device_faults
            WHERE device_id = :device_id
              AND at >= :boot_floor
            ORDER BY at, id
            LIMIT 1
       )
    """
)

COUNTS_SQL = text(
    """
    SELECT device_id, reason, count(*) AS n
      FROM device_faults
     WHERE device_id = ANY(:device_ids)
       AND at > :since
     GROUP BY device_id, reason
    """
)


def is_fault(reason: str | None) -> bool:
    """Whether a normalised `reset_reason` is one the fleet view counts."""
    return reason in FAULT_REASONS


async def record_fault(
    session: AsyncSession,
    *,
    device_id: str,
    reason: str,
    at: dt.datetime,
    max_rows: int = FAULT_MAX_ROWS_PER_DEVICE,
) -> bool:
    """Insert one fault row and trim the device to `max_rows`; `False` if not a fault.

    The caller decides "live announce of a registered device"; this decides "a fault".
    """
    if not is_fault(reason):
        return False
    await session.execute(insert(DeviceFault).values(device_id=device_id, at=at, reason=reason))
    await session.execute(TRIM_SQL, {"device_id": device_id, "keep": max_rows})
    return True


async def forget_reconnects(
    session: AsyncSession, *, device_id: str, received_at: dt.datetime, uptime_s: int
) -> int:
    """Delete this boot's repeated fault rows, keeping its earliest; return how many."""
    boot_floor = received_at - dt.timedelta(seconds=max(uptime_s, 0)) - UPTIME_RESOLUTION
    result = await session.execute(
        FORGET_RECONNECTS_SQL, {"device_id": device_id, "boot_floor": boot_floor}
    )
    count = getattr(result, "rowcount", 0)
    return count if isinstance(count, int) and count > 0 else 0


async def fault_counts(
    session: AsyncSession, *, device_ids: Sequence[str], since: dt.datetime
) -> dict[str, dict[str, int]]:
    """Fault rows after `since`, per device and reason. Devices with none are absent.

    `device_ids` comes from the caller (the ids its own list already read), the
    `deploys.latest_deploys` rule, so the read inherits the caller's cap.
    """
    rows = (
        await session.execute(COUNTS_SQL, {"device_ids": list(device_ids), "since": since})
    ).all()
    counts: dict[str, dict[str, int]] = {}
    for row in rows:
        counts.setdefault(row.device_id, {})[row.reason] = int(row.n)
    return counts

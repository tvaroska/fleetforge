"""The one clock. Both entrypoints import it; neither redefines it.

`now_utc()` used to live in `api/deps.py`, which the ingestor must not import
(`fleetforge.ingestor` has no HTTP server and pulling FastAPI's app factory into it
would drag its settings validation along). One definition, two processes.
"""

import datetime as dt


def now_utc() -> dt.datetime:
    """Timezone-aware UTC. Every timestamp column is TIMESTAMPTZ; naive comparison raises."""
    return dt.datetime.now(dt.UTC)


def device_time(received_at: dt.datetime, age_ms: int) -> dt.datetime:
    """When a device says something happened: `received_at - age_ms`, never its own clock.

    `spec/device-protocol.md` → *Clock*: an ESP32 boots at epoch zero, so the server never
    reads a device timestamp. A batched sample (`up/telemetry`, and `up/log` once built)
    carries `age_ms` instead, the time on the device's monotonic clock from the sample being
    taken to the payload being built (`docs/SWARM.md` 5.1). A negative age is read as 0.
    """
    return received_at - dt.timedelta(milliseconds=max(age_ms, 0))

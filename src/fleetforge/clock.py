"""The one clock. Both entrypoints import it; neither redefines it.

`now_utc()` used to live in `api/deps.py`, which the ingestor must not import
(`fleetforge.ingestor` has no HTTP server and pulling FastAPI's app factory into it
would drag its settings validation along). One definition, two processes.
"""

import datetime as dt


def now_utc() -> dt.datetime:
    """Timezone-aware UTC. Every timestamp column is TIMESTAMPTZ; naive comparison raises."""
    return dt.datetime.now(dt.UTC)

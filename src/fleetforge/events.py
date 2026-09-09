"""The `ff_events` NOTIFY channel: its name, its envelope, and the one emitter.

`DECISIONS.md` 2026-09-08 → *The ingestor is the only MQTT subscriber*: the ingestor
writes and `NOTIFY`s, and every API worker `LISTEN`s and fans out over SSE (R0-be-5).
That seam has exactly two halves, and if the channel name is spelled in two files the
SSE stream is silently empty with nothing failing loudly — so **R0-be-5 imports
`EVENTS_CHANNEL` and `DeviceEvent` from here** rather than restating either.

Three properties of `NOTIFY` that shape this module:

* **It takes no bind parameters.** `NOTIFY chan, 'payload'` built by string
  concatenation is a SQL injection carrying a device-controlled payload, so the only
  form used here is `SELECT pg_notify(:channel, :payload)`.
* **It is transactional.** Emitted on the caller's session inside the caller's
  transaction, before the commit that wrote the row — so a rolled-back write emits
  nothing and the SSE stream can never announce a row the database does not have.
* **The payload caps at 8000 bytes** and a longer one raises, which would take the
  write down with it. Hence `NOTIFY_PAYLOAD_LIMIT` and the skip below.

**The event is a hint, not a record.** It carries a *computed* `online` snapshot for
the SSE consumer; the client re-reads `GET /v1/devices`, because presence is derived
and a sleepy device goes offline with no event at all (`fleetforge.presence`).
"""

import datetime as dt
import logging
from enum import StrEnum

from pydantic import BaseModel
from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncSession

logger = logging.getLogger(__name__)

# The exact channel R0-be-5 LISTENs on. One constant, two processes.
EVENTS_CHANNEL = "ff_events"

# PostgreSQL's hard cap is 8000 bytes; stay well under it.
NOTIFY_PAYLOAD_LIMIT = 7_500


class EventType(StrEnum):
    """What changed. Additive only — an SSE client must ignore types it does not know."""

    # Emitted by `POST /v1/enroll` (R0-be-4) before the board has ever connected, so
    # its `online` is always false. R0-be-5 forwards it like any other.
    DEVICE_ENROLLED = "device.enrolled"
    DEVICE_ANNOUNCE = "device.announce"
    DEVICE_PRESENCE = "device.presence"
    DEVICE_HEARTBEAT = "device.heartbeat"
    # Any other `up/` channel, which at R0 only moves `last_seen`.
    DEVICE_SEEN = "device.seen"


class DeviceEvent(BaseModel):
    """The `ff_events` envelope.

    Deliberately small: the consumer re-reads `GET /v1/devices` for detail, and
    anything richer invites someone to treat the notification as the record of truth,
    which it must not be. `spec/prd.md` → *Timing* wants the dashboard to reflect a
    change within 2 s, which this trivially satisfies.
    """

    # Envelope version. Same evolution rule as the wire protocol: additive changes
    # only, and a reader ignores fields it does not know.
    v: int = 1
    type: EventType
    device_id: str
    # Server receipt time, never a device `ts` (`spec/device-protocol.md` → *Clock*).
    at: dt.datetime
    # Derived by `fleetforge.presence.is_online` at write time. A snapshot, not a cache.
    online: bool
    fw_version: str | None = None


NOTIFY_SQL = text("SELECT pg_notify(:channel, :payload)")


async def emit(session: AsyncSession, event: DeviceEvent) -> None:
    """Queue `event` on `ff_events` inside the caller's transaction.

    The caller commits; an uncommitted or rolled-back transaction delivers nothing.
    An oversized payload is dropped with a WARNING rather than allowed to raise: a
    future field addition should degrade to "no live update", never to "no ingestion".
    The body is never logged (same rule as the ingestor's message loop).
    """
    payload = event.model_dump_json()
    if len(payload.encode("utf-8")) > NOTIFY_PAYLOAD_LIMIT:
        logger.warning(
            "event for device %s exceeds the %d byte notify limit; not emitting",
            event.device_id,
            NOTIFY_PAYLOAD_LIMIT,
        )
        return
    await session.execute(NOTIFY_SQL, {"channel": EVENTS_CHANNEL, "payload": payload})

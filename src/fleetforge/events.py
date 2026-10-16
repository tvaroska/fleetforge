"""The `ff_events` NOTIFY channel: its name, its envelope, and the one emitter.

`DECISIONS.md` 2026-09-08 → *The ingestor is the only MQTT subscriber*: the ingestor
writes and `NOTIFY`s, and every API worker `LISTEN`s and fans out over SSE (R0-be-5).
That seam has exactly two halves, and if the channel name is spelled in two files the
SSE stream is silently empty with nothing failing loudly — so the consumer,
`fleetforge.api.eventstream` (R0-be-5), **imports `EVENTS_CHANNEL`, `DeviceEvent` and
`NOTIFY_PAYLOAD_LIMIT` from here** rather than restating any of them.

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
`online` is `null` on exactly one event: a live `device.announce` whose session has not
reported presence yet (S0-bug-2), so a connecting board is never snapshotted offline.
"""

import datetime as dt
import logging
from enum import StrEnum

from pydantic import BaseModel
from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncSession

from fleetforge.selftest_outcome import SelfTestOutcome

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
    # A live announce opens a new broker session, so its `online` is `true` or `null`
    # ("this session's `up/presence` follows"), never `false` (S0-bug-2). A retained
    # replay still reads the derived value.
    DEVICE_ANNOUNCE = "device.announce"
    DEVICE_PRESENCE = "device.presence"
    DEVICE_HEARTBEAT = "device.heartbeat"
    # Any other live `up/` message: telemetry and log (R4-be-1 stores them; the type
    # stays `device.seen`, since every frame already triggers the dashboard's re-read and
    # no new type is needed), an unknown channel from a future agent — and an
    # `up/status` that recorded no row (no `cmd_id`, or a retained replay of a
    # transition already on file).
    DEVICE_SEEN = "device.seen"
    # An `up/status` that actually wrote a `deploy_events` row (R1-be-4). Emitted only
    # when a row was written — a deduped replay is not news. Since R4-be-2 the frame
    # carries `cmd_id`, `state`, `is_terminal` and a terminal `detail` for non-browser
    # waiters; the dashboard still only re-reads (`fleet.ts` rule 1).
    DEVICE_DEPLOY = "device.deploy"
    # A boot/enrol stage posted to `POST /v1/device-progress` (S0-fw-1). The only
    # event type whose `device_id` may name a board that is not in `devices` at all —
    # that is the point of it. The stage itself is NOT in the envelope: it is
    # device-controlled text, and the consumer re-reads like every other type.
    DEVICE_PROGRESS = "device.progress"

    # `PATCH /v1/devices/{id}` (R2b-be-1) changed an operator-set fact: name or group.
    # The new values are NOT in the envelope: the name is operator text, and the
    # consumer re-reads like every other type.
    DEVICE_UPDATED = "device.updated"


class DeviceEvent(BaseModel):
    """The `ff_events` envelope.

    Deliberately small: the consumer re-reads `GET /v1/devices` for detail, and
    anything richer invites someone to treat the notification as the record of truth,
    which it must not be. The deploy fields (R4-be-2) are the transaction *key and
    outcome*, still not the record; `GET /v1/devices` is. `spec/prd.md` → *Timing* wants the dashboard to reflect a
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
    # `None` only on a live `device.announce` whose session has not reported presence
    # yet (S0-bug-2). Required: every emitter must still decide.
    online: bool | None
    fw_version: str | None = None
    # R4-be-1: on `device.announce` only, the board's normalised `reset_reason` as stored
    # (`devices.last_reset_reason`, retained replays included); `null` on every other type
    # and when not reported. Already free of control characters, and
    # `api/eventstream._on_notify` refuses a raw newline anyway. Additive, so `v` stays 1.
    reset_reason: str | None = None
    # R4-be-2: on `device.deploy` only, the transaction the written `deploy_events` row
    # belongs to, so a CI or test runner can wait on a frame instead of polling
    # `GET /v1/devices`. `null` on every other type. `cmd_id` and `state` are device-
    # controlled text as recorded; `is_terminal` is the server's answer
    # (`TERMINAL_DEPLOY_STATES` at write time) so no reader re-derives the vocabulary;
    # `detail` only on a terminal row, and only the stored, sanitised text
    # (`deploys.snapshot_detail`), never the wire's. Additive, so `v` stays 1.
    cmd_id: str | None = None
    state: str | None = None
    is_terminal: bool | None = None
    detail: str | None = None
    # R5-be-1: on `device.deploy` only, on a terminal row only (the `detail` rule), the
    # custom self-test's outcome parsed from the stored, sanitised detail by
    # `selftest_outcome.selftest_outcome` (grammar plus state rule). `null` otherwise.
    # `reason` / `returned_to` are device-controlled text. Additive, so `v` stays 1.
    selftest: SelfTestOutcome | None = None


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

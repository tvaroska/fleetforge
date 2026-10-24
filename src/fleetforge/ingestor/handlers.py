"""One inbound MQTT message → the right `UPDATE` → one `ff_events` notification.

The entry point is `handle_up_message`, and the ordering inside it is load-bearing.
Two rules from `spec/device-protocol.md` decide almost everything here, and both are
about *not* believing something is alive:

* **A retained message is a replay, not a sign of life.** `announce`, `presence` and
  `status` are retained, and the ingestor re-`subscribe`s on every connect — so every
  reconnect replays the whole retained set, for every board, including ones that died
  a week ago. MQTT 3.1.1 only sets the delivery `retain` flag for a message served out
  of the retained store in response to a new subscription, so that flag is exactly the
  discriminator: apply the **state**, never touch `last_seen`.
* **The LWT is published by the broker, not the device.** `presence{"online":false}`
  is the broker acting, and recording it as evidence of device life is wrong
  everywhere — catastrophic for `sleepy`, where "the LWT fires on every normal sleep
  and means nothing" and a bricked e-paper frame would stay online forever.

* **Retained `up/status` is still ingested** — unlike telemetry and log, which are
  dropped when retained. The retained value is exactly how an outcome published while
  the ingestor was down gets delivered at all, and there is no manual ack: a dropped
  status is an outcome lost forever. It proves no liveness, though, so the state is
  recorded and `last_seen` stays where it was. The duplicate a replay would otherwise
  create is the writer's problem, and `deploys.record_observed_status` deduplicates on
  `(device_id, cmd_id, state)`.

Together: **`last_seen` advances only on a live (`retain=False`) message that is not
`presence{online:false}`.**

**A live announce never reports `online: false`.** It opens a new broker session, and the
stored `presence_reported` still describes the previous one, so the event says `null`
("this session's `up/presence` follows") unless presence already derives online
(S0-bug-2). A retained announce replay keeps the derived value.

**The ingestor's INSERTs**, each only for a live (or, for profiles, any) message of a
registered device, each in its own SAVEPOINT so a failure is logged and never costs the
message's `last_seen` or event (`_savepoint`):

* a partition table nobody knows becomes one pending `partition_profiles` row
  (R3-be-2, `partition_profiles.note_detected`, idempotent and capped; retained
  announces too, a detected map is state);
* a live announce whose `reset_reason` is a fault becomes a `device_faults` row, and a
  live heartbeat's `uptime_s` deletes the same boot's reconnect repeats
  (R4-be-1, `fleetforge.faults`);
* a live `up/telemetry` batch becomes `device_telemetry` rows and moves
  `device_metrics` forward (R4-be-1, `fleetforge.telemetry_store`);
* a live `up/log` batch becomes `device_logs` rows, trimmed to the newest 5 000 per
  device in the same SAVEPOINT (R4-be-1, `fleetforge.device_logs`). Log text is never
  written into this process's log: counts only.

And one UPDATE of the same shape (R5-be-2): a live `up/ack` records the board's first
answer on the `device_commands` row of the verb it answers (`device_commands.record_answer`,
first answer wins, matched on the sending board). An ack never creates a row, never writes
`deploy_events` (it is not a transaction state), and its `detail` is never logged.

`store.py` still never INSERTs: its rule is about `devices`, the enrollment boundary.

This module does not commit — the caller owns the transaction, which is what lets the
test suite drive it through the rolled-back `session` fixture.
"""

import datetime as dt
import logging
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager

from sqlalchemy.exc import SQLAlchemyError
from sqlalchemy.ext.asyncio import AsyncSession

from fleetforge import device_commands, device_logs, faults, partition_profiles, telemetry_store
from fleetforge.broker import CommandPublisher
from fleetforge.clock import device_time
from fleetforge.config import Settings
from fleetforge.db.models import DeployEvent, Device
from fleetforge.deploys import record_observed_status, snapshot_detail
from fleetforge.events import DeviceEvent, EventType, emit
from fleetforge.ingestor import otel, store
from fleetforge.ingestor.protocol import (
    ACK_MAX_BYTES,
    LOG_MAX_BYTES,
    TELEMETRY_MAX_BYTES,
    AckPayload,
    AnnouncePayload,
    HeartbeatPayload,
    LogPayload,
    PresencePayload,
    StatusPayload,
    TelemetryPayload,
    UpChannel,
    decode,
    parse_up_topic,
)
from fleetforge.presence import is_online
from fleetforge.selftest_outcome import selftest_outcome

logger = logging.getLogger(__name__)


async def handle_up_message(
    session: AsyncSession,
    *,
    topic: str,
    payload: bytes,
    retained: bool,
    received_at: dt.datetime,
    tolerance: float,
    publisher: CommandPublisher | None = None,
    settings: Settings | None = None,
) -> DeviceEvent | None:
    """Ingest one `up/` message; return the event emitted, or `None` if it was dropped.

    `received_at` is the server's receipt time, captured once by the caller and
    threaded through: an ESP32 boots at epoch zero and only fixes its clock after
    SNTP, so a device `ts` is ordering information at best and is never read here
    (`spec/device-protocol.md` → *Clock*).
    """
    parsed = parse_up_topic(topic)
    if parsed is None:
        logger.debug("ignoring message on unparseable topic %s", topic)
        return None

    if not payload:
        # A zero-length retained publish is how a retained topic is *cleared*
        # (`spec/device-protocol.md` → *Decommissioning*). Not an error, and it must
        # not produce a decode warning on every decommission.
        return None

    device_id = parsed.device_id
    row = None
    event_type = EventType.DEVICE_SEEN
    status: StatusPayload | None = None

    if parsed.channel == UpChannel.ANNOUNCE:
        announce = decode(AnnouncePayload, topic, payload)
        if announce is None:
            return None
        if announce.device_id is not None and announce.device_id != device_id:
            # The topic is authoritative: `pattern write ff/v1/d/%u/up/#` binds it to
            # the broker username, so a disagreeing body is an impersonation attempt.
            logger.warning(
                "dropping announce on %s: payload claims device_id %s",
                topic,
                announce.device_id,
            )
            return None
        row = await store.apply_announce(
            session, device_id, announce, received_at, advance_last_seen=not retained
        )
        event_type = EventType.DEVICE_ANNOUNCE
        if row is not None:
            if not retained:
                # S0-fw-6: logged on a live announce only: a retained replay repeats an
                # old boot's reason on every ingestor reconnect. `None` covers absent
                # (agents <= 0.5.0), null and malformed alike. Stored on every announce
                # since R4-be-1 (`store.py::_reset_values`), and counted as a fault here,
                # live only, for the same replay reason.
                logger.info(
                    "device %s announced reset_reason=%s",
                    device_id,
                    announce.reset_reason if announce.reset_reason is not None else "null",
                )
                if announce.reset_reason is not None and faults.is_fault(announce.reset_reason):
                    async with _savepoint(session, device_id, "fault bookkeeping", "announce"):
                        await faults.record_fault(
                            session,
                            device_id=device_id,
                            reason=announce.reset_reason,
                            at=received_at,
                        )
            # Retained replays too: a detected map is state, not liveness.
            await _note_detected_profile(session, row)

    elif parsed.channel == UpChannel.PRESENCE:
        presence = decode(PresencePayload, topic, payload)
        if presence is None or presence.online is None:
            return None
        # The whole retained/LWT rule in one expression: a replay proves nothing, and
        # `{"online":false}` came from the broker rather than the board.
        row = await store.apply_presence(
            session,
            device_id,
            presence.online,
            received_at,
            advance_last_seen=not retained and presence.online,
        )
        event_type = EventType.DEVICE_PRESENCE

    elif parsed.channel == UpChannel.HB:
        heartbeat = decode(HeartbeatPayload, topic, payload)
        if heartbeat is None:
            return None
        # Heartbeats are QoS 1, retain no — so `retained` is always False here. Passed
        # through anyway rather than assumed, for uniformity with the other branches.
        row = await store.apply_heartbeat(
            session,
            device_id,
            heartbeat.fw_version,
            received_at,
            advance_last_seen=not retained,
            health=heartbeat,
        )
        event_type = EventType.DEVICE_HEARTBEAT
        if row is not None and not retained and heartbeat.uptime_s is not None:
            # `uptime_s` dates this boot; fault rows inside it but its first were
            # reconnect announces (`fleetforge.faults` has the safety argument).
            async with _savepoint(session, device_id, "fault reconnect dedupe", "heartbeat"):
                await faults.forget_reconnects(
                    session,
                    device_id=device_id,
                    received_at=received_at,
                    uptime_s=heartbeat.uptime_s,
                )

    elif parsed.channel == UpChannel.STATUS:
        status = decode(StatusPayload, topic, payload)
        if status is None:
            return None
        # The one channel whose retained replay is ingested rather than dropped: it is
        # how an outcome published while the ingestor was down arrives at all. A replay
        # is still not a sign of life, so only a live status moves `last_seen`. Either
        # way the row has to be looked up — an insert for an unregistered device would
        # hit `deploy_events`' `ON DELETE RESTRICT` FK and be swallowed as "ingest
        # failed", losing the write — so the unregistered case keeps one drop path.
        row = (
            await store.fetch_live_device(session, device_id)
            if retained
            else await store.touch_last_seen(session, device_id, received_at)
        )

    else:
        # `up/telemetry`, `up/log` (R4-fw-2, R4-be-1) and `up/ack` (R5-be-2) are decoded
        # and stored below; an unknown channel from a future agent lands here too. All of
        # them prove the board is alive, and `last_seen` moves before any decode, so an
        # undecodable payload still counts. A retained one is dropped before the device
        # lookup: an ack is never accepted from a replay. **No `deploy_events` row is
        # written for any of them.**
        if retained:
            logger.debug("ignoring retained replay on %s", topic)
            return None
        row = await store.touch_last_seen(session, device_id, received_at)

    if row is None:
        logger.info("ignoring message from unregistered or decommissioned device %s", device_id)
        return None

    if parsed.channel == UpChannel.TELEMETRY:
        # Live only (a retained replay returned above) and registered only.
        await _store_telemetry(session, device_id, topic, payload, received_at, device=row)
    elif parsed.channel == UpChannel.LOG:
        await _store_log(session, device_id, topic, payload, received_at, device=row)

    answered: str | None = None
    if parsed.channel == UpChannel.ACK:
        # Live only and registered only, like the two above. The event is upgraded only
        # when an answer was recorded: a duplicate or an unknown id is not news.
        answered = await _store_ack(session, device_id, topic, payload, received_at)
        if answered is not None:
            event_type = EventType.DEVICE_COMMAND

    written: DeployEvent | None = None
    if status is not None and status.cmd_id and status.state:
        # A status with no `cmd_id` (or no `state`) belongs to no transaction — `idle`
        # with a null `cmd_id` is a board saying "nothing in flight" — and recording it
        # would add a row per boot to a table kept forever. The event is upgraded only
        # when a row was actually written: a deduped replay is not news.
        written = await record_observed_status(
            session,
            device_id=device_id,
            cmd_id=status.cmd_id,
            state=status.state,
            at=received_at,
            pct=status.pct,
            detail=status.detail,
        )
        if written is not None:
            event_type = EventType.DEVICE_DEPLOY

    # Bound once: the terminal row's stored detail feeds both `detail` and `selftest`.
    terminal_detail = (
        snapshot_detail(written.detail) if written is not None and written.is_terminal else None
    )
    event = DeviceEvent(
        type=event_type,
        device_id=device_id,
        project_id=row.project_id,
        at=received_at,
        online=_online_snapshot(
            row,
            live_announce=parsed.channel == UpChannel.ANNOUNCE and not retained,
            now=received_at,
            tolerance=tolerance,
        ),
        fw_version=row.fw_version,
        # The stored (normalised) value, retained replays included; every other type
        # sends null (R4-be-1).
        reset_reason=row.last_reset_reason if event_type is EventType.DEVICE_ANNOUNCE else None,
        # The written row's transaction (R4-be-2), never `status.detail` off the wire:
        # the stored copy is sanitised (signed URLs redacted), and only a terminal row's.
        cmd_id=written.cmd_id if written is not None else None,
        state=written.state if written is not None else None,
        is_terminal=written.is_terminal if written is not None else None,
        detail=terminal_detail,
        # R5-be-1: the self-test outcome, parsed from that same stored text, terminal row only.
        selftest=(
            selftest_outcome(written.state, terminal_detail)
            if written is not None and written.is_terminal
            else None
        ),
        # R5-be-2: the server-minted id of the command whose answer was just recorded.
        command_id=answered,
    )
    await emit(session, event)
    return event


@asynccontextmanager
async def _savepoint(
    session: AsyncSession, device_id: str, what: str, kept: str
) -> AsyncIterator[None]:
    """Run the body in a SAVEPOINT; on failure log a WARNING and carry on.

    The `_note_detected_profile` shape, shared by R4-be-1's writes: a failure (including
    a new-image ingestor on a schema the api has not migrated yet, `UndefinedTableError`)
    rolls back only the nested statements, and the message's own write and event stand.
    """
    try:
        async with session.begin_nested():
            yield
    except SQLAlchemyError:
        logger.warning("device %s: %s failed; %s kept", device_id, what, kept, exc_info=True)


async def _store_telemetry(
    session: AsyncSession,
    device_id: str,
    topic: str,
    payload: bytes,
    received_at: dt.datetime,
    *,
    device: Device | None = None,
) -> None:
    """Decode an `up/telemetry` batch, store it, and log a summary; never raises.

    Each sample is stamped `received_at - age_ms` (`clock.device_time`), the device's
    clock never read. Names are regex-validated and values are numbers, so nothing
    free-text from the board reaches this log or the table.
    """
    if len(payload) > TELEMETRY_MAX_BYTES:
        logger.warning(
            "device %s: telemetry payload of %d bytes dropped (over %d)",
            device_id,
            len(payload),
            TELEMETRY_MAX_BYTES,
        )
        return
    telemetry = decode(TelemetryPayload, topic, payload)
    if telemetry is None or (not telemetry.samples and not telemetry.unusable):
        return
    logger.info(
        "device %s telemetry samples=%d dropped=%d unusable=%d oldest_age_ms=%d",
        device_id,
        len(telemetry.samples),
        telemetry.dropped or 0,
        telemetry.unusable,
        max((sample.age_ms for sample in telemetry.samples), default=0),
    )
    for sample in telemetry.samples:
        logger.debug(
            "device %s metric %s=%r at %s",
            device_id,
            sample.name,
            sample.value,
            device_time(received_at, sample.age_ms).isoformat(),
        )
    if telemetry.samples:
        async with _savepoint(session, device_id, "telemetry storage", "liveness"):
            await telemetry_store.record_samples(
                session, device_id=device_id, samples=telemetry.samples, received_at=received_at
            )
        if device is not None:
            async with _savepoint(session, device_id, "otel metrics export", "liveness"):
                await otel.enqueue_metrics_to_otel(
                    session,
                    device=device,
                    samples=telemetry.samples,
                    received_at=received_at,
                )


async def _store_log(
    session: AsyncSession,
    device_id: str,
    topic: str,
    payload: bytes,
    received_at: dt.datetime,
    *,
    device: Device | None = None,
) -> None:
    """Decode an `up/log` batch and store it, bounded; never raises, never logs `text`.

    The one INFO line carries counts only: the lines are device-controlled text, and this
    process's log is not a second, unsanitised copy of the table.
    """
    if len(payload) > LOG_MAX_BYTES:
        logger.warning(
            "device %s: log payload of %d bytes dropped (over %d)",
            device_id,
            len(payload),
            LOG_MAX_BYTES,
        )
        return
    batch = decode(LogPayload, topic, payload)
    if batch is None or (not batch.lines and not batch.unusable and not batch.dropped):
        return
    logger.info(
        "device %s log lines=%d dropped=%d unusable=%d",
        device_id,
        len(batch.lines),
        batch.dropped or 0,
        batch.unusable,
    )
    async with _savepoint(session, device_id, "log storage", "liveness"):
        await device_logs.record_log_batch(
            session, device_id=device_id, payload=batch, received_at=received_at
        )
    if device is not None and batch.lines:
        async with _savepoint(session, device_id, "otel logs export", "liveness"):
            await otel.enqueue_logs_to_otel(
                session,
                device=device,
                lines=batch.lines,
                dropped=batch.dropped or 0,
                received_at=received_at,
            )


async def _store_ack(
    session: AsyncSession, device_id: str, topic: str, payload: bytes, received_at: dt.datetime
) -> str | None:
    """Decode an `up/ack` and keep it as the first answer to the command it names; return
    that command's id when an answer was recorded, else None. Never raises, never logs the
    `detail` (device-controlled text), never writes `deploy_events`."""
    if len(payload) > ACK_MAX_BYTES:
        logger.warning(
            "device %s: ack payload of %d bytes dropped (over %d)",
            device_id,
            len(payload),
            ACK_MAX_BYTES,
        )
        return None
    ack = decode(AckPayload, topic, payload)
    if ack is None:
        return None
    # The `_savepoint` shape, spelled out because the result decides the event type: a
    # failure (an un-migrated schema included) must not upgrade it to `device.command`.
    try:
        async with session.begin_nested():
            recorded = await device_commands.record_answer(
                session,
                device_id=device_id,
                command_id=ack.id,
                type=ack.type,
                result=ack.result,
                cmd_id=ack.cmd_id,
                detail=ack.detail,
                at=received_at,
            )
    except SQLAlchemyError:
        logger.warning("device %s: ack storage failed; liveness kept", device_id, exc_info=True)
        return None
    return recorded.command_id if recorded is not None else None


def _online_snapshot(
    row: Device, *, live_announce: bool, now: dt.datetime, tolerance: float
) -> bool | None:
    """`is_online`, except that a live announce withholds a `False`.

    A live announce is a new session whose `up/presence` has not arrived, while
    `presence_reported` still holds the previous session's value (NULL after enroll,
    false after an LWT). Reporting `False` would be a stale answer, so it reports
    `None`. `presence.is_online` stays the one derivation; this only withholds it.
    """
    online = is_online(row, now=now, tolerance=tolerance)
    if live_announce and not online:
        return None
    return online


async def _note_detected_profile(session: AsyncSession, row: Device) -> None:
    """Record an unknown partition table as a pending profile; never lose the announce.

    The SAVEPOINT is what makes "never" true: a failure (including a new-image ingestor on
    a schema the api has not migrated yet, `UndefinedTableError`) rolls back only this
    statement, and the announce already applied in the outer transaction stands. No event:
    the same transaction emits `device.announce`, the hint to re-read the profiles.
    """
    device_id = row.device_id
    try:
        async with session.begin_nested():
            await partition_profiles.note_detected(session, row)
    except SQLAlchemyError:
        logger.warning(
            "device %s: detected-profile bookkeeping failed; announce kept",
            device_id,
            exc_info=True,
        )

"""One inbound MQTT / relayed `up/` message -> the right `UPDATE` -> one `ff_events` notification.

Shared across `fleetforge.ingestor` and `fleetforge.api` (`A-29`) with decomposed per-channel
dispatch helpers (`A-32`). Callers own the transaction and commit.
"""

import datetime as dt
import logging
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager

from sqlalchemy.exc import SQLAlchemyError
from sqlalchemy.ext.asyncio import AsyncSession

from fleetforge import device_commands, device_logs, faults, partition_profiles, telemetry_store
from fleetforge.clock import device_time
from fleetforge.config import Settings
from fleetforge.db.models import DeployEvent, Device
from fleetforge.deploys import record_observed_status, snapshot_detail
from fleetforge.events import DeviceEvent, EventType, emit
from fleetforge.ingest import otel, store
from fleetforge.ingest.protocol import (
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
from fleetforge.ingest.rate_budget import DeviceIngestRateLimiter
from fleetforge.presence import is_online
from fleetforge.selftest_outcome import selftest_outcome

logger = logging.getLogger(__name__)


async def _handle_announce(
    session: AsyncSession,
    *,
    device_id: str,
    topic: str,
    payload: bytes,
    retained: bool,
    received_at: dt.datetime,
) -> tuple[Device | None, EventType, StatusPayload | None]:
    announce = decode(AnnouncePayload, topic, payload)
    if announce is None:
        return None, EventType.DEVICE_ANNOUNCE, None
    if announce.device_id is not None and announce.device_id != device_id:
        logger.warning(
            "dropping announce on %s: payload claims device_id %s",
            topic,
            announce.device_id,
        )
        return None, EventType.DEVICE_ANNOUNCE, None
    row = await store.apply_announce(
        session, device_id, announce, received_at, advance_last_seen=not retained
    )
    if row is not None:
        if not retained:
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
        await _note_detected_profile(session, row)
    return row, EventType.DEVICE_ANNOUNCE, None


async def _handle_heartbeat(
    session: AsyncSession,
    *,
    device_id: str,
    topic: str,
    payload: bytes,
    retained: bool,
    received_at: dt.datetime,
) -> tuple[Device | None, EventType, StatusPayload | None]:
    heartbeat = decode(HeartbeatPayload, topic, payload)
    if heartbeat is None:
        return None, EventType.DEVICE_HEARTBEAT, None
    row = await store.apply_heartbeat(
        session,
        device_id,
        heartbeat.fw_version,
        received_at,
        advance_last_seen=not retained,
        health=heartbeat,
    )
    if row is not None and not retained and heartbeat.uptime_s is not None:
        async with _savepoint(session, device_id, "fault reconnect dedupe", "heartbeat"):
            await faults.forget_reconnects(
                session,
                device_id=device_id,
                received_at=received_at,
                uptime_s=heartbeat.uptime_s,
            )
    return row, EventType.DEVICE_HEARTBEAT, None


async def _dispatch_primary_channel(
    session: AsyncSession,
    *,
    channel: str,
    device_id: str,
    topic: str,
    payload: bytes,
    retained: bool,
    received_at: dt.datetime,
) -> tuple[Device | None, EventType, StatusPayload | None, bool]:
    """Apply primary device state write; returns `(row, event_type, status, dropped_early)`."""
    if channel == UpChannel.ANNOUNCE:
        row, ev_type, st = await _handle_announce(
            session,
            device_id=device_id,
            topic=topic,
            payload=payload,
            retained=retained,
            received_at=received_at,
        )
        return row, ev_type, st, row is None and decode(AnnouncePayload, topic, payload) is None
    if channel == UpChannel.PRESENCE:
        presence = decode(PresencePayload, topic, payload)
        if presence is None or presence.online is None:
            return None, EventType.DEVICE_PRESENCE, None, True
        row = await store.apply_presence(
            session,
            device_id,
            presence.online,
            received_at,
            advance_last_seen=not retained and presence.online,
        )
        return row, EventType.DEVICE_PRESENCE, None, False
    if channel == UpChannel.HB:
        row, ev_type, st = await _handle_heartbeat(
            session,
            device_id=device_id,
            topic=topic,
            payload=payload,
            retained=retained,
            received_at=received_at,
        )
        return row, ev_type, st, row is None and decode(HeartbeatPayload, topic, payload) is None
    if channel == UpChannel.STATUS:
        status = decode(StatusPayload, topic, payload)
        if status is None:
            return None, EventType.DEVICE_SEEN, None, True
        row = (
            await store.fetch_live_device(session, device_id)
            if retained
            else await store.touch_last_seen(session, device_id, received_at)
        )
        return row, EventType.DEVICE_SEEN, status, False
    if retained:
        logger.debug("ignoring retained replay on %s", topic)
        return None, EventType.DEVICE_SEEN, None, True
    row = await store.touch_last_seen(session, device_id, received_at)
    return row, EventType.DEVICE_SEEN, None, False


async def _apply_secondary_payloads(
    session: AsyncSession,
    *,
    channel: str,
    device_id: str,
    topic: str,
    payload: bytes,
    received_at: dt.datetime,
    row: Device,
    status: StatusPayload | None,
    event_type: EventType,
    settings: Settings | None = None,
) -> tuple[EventType, str | None, DeployEvent | None]:
    if channel == UpChannel.TELEMETRY:
        await _store_telemetry(
            session, device_id, topic, payload, received_at, device=row, settings=settings
        )
    elif channel == UpChannel.LOG:
        await _store_log(
            session, device_id, topic, payload, received_at, device=row, settings=settings
        )

    answered: str | None = None
    if channel == UpChannel.ACK:
        answered = await _store_ack(session, device_id, topic, payload, received_at)
        if answered is not None:
            event_type = EventType.DEVICE_COMMAND

    written: DeployEvent | None = None
    if status is not None and status.cmd_id and status.state:
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
    return event_type, answered, written


async def handle_up_message(
    session: AsyncSession,
    *,
    topic: str,
    payload: bytes,
    retained: bool,
    received_at: dt.datetime,
    tolerance: float,
    settings: Settings | None = None,
    rate_limiter: DeviceIngestRateLimiter | None = None,
) -> DeviceEvent | None:
    """Ingest one `up/` message; return the event emitted, or `None` if it was dropped."""
    parsed = parse_up_topic(topic)
    if parsed is None:
        logger.debug("ignoring message on unparseable topic %s", topic)
        return None
    if not payload:
        return None
    if retained and parsed.channel in (UpChannel.LOG, UpChannel.TELEMETRY):
        logger.debug("ignoring retained replay on %s", topic)
        return None

    device_id = parsed.device_id
    if rate_limiter is not None and not rate_limiter.allow(device_id, parsed.channel):
        return None

    row, event_type, status, dropped_early = await _dispatch_primary_channel(
        session,
        channel=parsed.channel,
        device_id=device_id,
        topic=topic,
        payload=payload,
        retained=retained,
        received_at=received_at,
    )
    if dropped_early:
        return None
    if row is None:
        logger.info("ignoring message from unregistered or decommissioned device %s", device_id)
        return None

    event_type, answered, written = await _apply_secondary_payloads(
        session,
        channel=parsed.channel,
        device_id=device_id,
        topic=topic,
        payload=payload,
        received_at=received_at,
        row=row,
        status=status,
        event_type=event_type,
        settings=settings,
    )

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
        reset_reason=row.last_reset_reason if event_type is EventType.DEVICE_ANNOUNCE else None,
        cmd_id=written.cmd_id if written is not None else None,
        state=written.state if written is not None else None,
        is_terminal=written.is_terminal if written is not None else None,
        detail=terminal_detail,
        selftest=(
            selftest_outcome(written.state, terminal_detail)
            if written is not None and written.is_terminal
            else None
        ),
        command_id=answered,
    )
    await emit(session, event)
    return event


@asynccontextmanager
async def _savepoint(
    session: AsyncSession, device_id: str, what: str, kept: str
) -> AsyncIterator[None]:
    """Run the body in a SAVEPOINT; on failure log a WARNING and carry on."""
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
    settings: Settings | None = None,
) -> None:
    """Decode an `up/telemetry` batch, store it, and log a summary; never raises."""
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
            await otel.enqueue_metrics_to_otel(
                session,
                device=device,
                samples=telemetry.samples,
                received_at=received_at,
                settings=settings,
            )


async def _store_log(
    session: AsyncSession,
    device_id: str,
    topic: str,
    payload: bytes,
    received_at: dt.datetime,
    *,
    device: Device | None = None,
    settings: Settings | None = None,
) -> None:
    """Decode an `up/log` batch and store it, bounded; never raises, never logs `text`."""
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
            session,
            device_id=device_id,
            payload=batch,
            received_at=received_at,
            context=otel.device_snapshot(device),
        )
    if device is not None and batch.lines:
        await otel.enqueue_logs_to_otel(
            session,
            device=device,
            lines=batch.lines,
            dropped=batch.dropped or 0,
            received_at=received_at,
            settings=settings,
        )


async def _store_ack(
    session: AsyncSession, device_id: str, topic: str, payload: bytes, received_at: dt.datetime
) -> str | None:
    """Decode an `up/ack` and keep it as the first answer to the command it names."""
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
    """`is_online`, except that a live announce withholds a `False`."""
    online = is_online(row, now=now, tolerance=tolerance)
    if live_announce and not online:
        return None
    return online


async def _note_detected_profile(session: AsyncSession, row: Device) -> None:
    """Record an unknown partition table as a pending profile; never lose the announce."""
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

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

This module does not commit — the caller owns the transaction, which is what lets the
test suite drive it through the rolled-back `session` fixture.
"""

import datetime as dt
import logging

from sqlalchemy.ext.asyncio import AsyncSession

from fleetforge.deploys import record_observed_status
from fleetforge.events import DeviceEvent, EventType, emit
from fleetforge.ingestor import store
from fleetforge.ingestor.protocol import (
    AnnouncePayload,
    HeartbeatPayload,
    PresencePayload,
    StatusPayload,
    UpChannel,
    decode,
    parse_up_topic,
)
from fleetforge.presence import is_online

logger = logging.getLogger(__name__)


async def handle_up_message(
    session: AsyncSession,
    *,
    topic: str,
    payload: bytes,
    retained: bool,
    received_at: dt.datetime,
    tolerance: float,
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
        )
        event_type = EventType.DEVICE_HEARTBEAT

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
        # `up/telemetry` → **R4**; `up/log` → R4 and still an open item in the protocol
        # doc. An unknown channel from a future agent lands here too. All of them only
        # prove the board is alive: **no `deploy_events` row is written for them.**
        if retained:
            logger.debug("ignoring retained replay on %s", topic)
            return None
        row = await store.touch_last_seen(session, device_id, received_at)

    if row is None:
        logger.info("ignoring message from unregistered or decommissioned device %s", device_id)
        return None

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

    event = DeviceEvent(
        type=event_type,
        device_id=device_id,
        at=received_at,
        online=is_online(row, now=received_at, tolerance=tolerance),
        fw_version=row.fw_version,
    )
    await emit(session, event)
    return event

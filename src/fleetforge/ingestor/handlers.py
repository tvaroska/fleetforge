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

Together: **`last_seen` advances only on a live (`retain=False`) message that is not
`presence{online:false}`.**

This module does not commit — the caller owns the transaction, which is what lets the
test suite drive it through the rolled-back `session` fixture.
"""

import datetime as dt
import logging

from sqlalchemy.ext.asyncio import AsyncSession

from fleetforge.events import DeviceEvent, EventType, emit
from fleetforge.ingestor import store
from fleetforge.ingestor.protocol import (
    AnnouncePayload,
    HeartbeatPayload,
    PresencePayload,
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

    else:
        # `up/status` → `deploy_events` in **R1**; `up/telemetry` → **R3**; `up/log` →
        # R3 and still an open item in the protocol doc. An unknown channel from a
        # future agent lands here too. At R0 all of them only prove the board is
        # alive: **no `deploy_events` row is written in this task.**
        if retained:
            logger.debug("ignoring retained replay on %s", topic)
            return None
        row = await store.touch_last_seen(session, device_id, received_at)

    if row is None:
        logger.info("ignoring message from unregistered or decommissioned device %s", device_id)
        return None

    event = DeviceEvent(
        type=event_type,
        device_id=device_id,
        at=received_at,
        online=is_online(row, now=received_at, tolerance=tolerance),
        fw_version=row.fw_version,
    )
    await emit(session, event)
    return event

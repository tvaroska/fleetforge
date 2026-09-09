"""The ingest path, against the migrated test database.

Every case is named for the failure it prevents. Three of them are the ones that
matter most, and each has a comment saying why:

* an unregistered device must create **nothing** — the enrollment boundary;
* a retained replay must not advance `last_seen` — otherwise an ingestor restart
  marks the entire dead fleet alive;
* `presence{"online":false}` must not advance `last_seen` — that message comes from
  the broker's LWT, not the board.
"""

import asyncio
import datetime as dt
import json
from collections.abc import AsyncIterator

import asyncpg
import pytest
from sqlalchemy import func, select, text
from sqlalchemy.ext.asyncio import AsyncEngine, AsyncSession

from fleetforge.db.base import asyncpg_dsn
from fleetforge.db.models import DeployEvent, Device
from fleetforge.events import EVENTS_CHANNEL, DeviceEvent, EventType
from fleetforge.ingestor.handlers import handle_up_message
from tests.conftest import TEST_DB_NAME, database_url_for

DEVICE_ID = "a4cf12b3de90"
NOW = dt.datetime(2026, 9, 8, 12, 0, tzinfo=dt.UTC)
TOLERANCE = 2.5

ANNOUNCE = {
    "proto": 1,
    "device_id": DEVICE_ID,
    "platform_type": "esp32c6",
    "fw_version": "1.4.2",
    "agent_version": "0.3.0",
    "link_type": "wifi",
    "power_class": "always_on",
    "partition_layout": "ab-4m-v1",
    "ota_slot_size": 1966080,
    "capabilities": ["ota", "selftest"],
}


async def seed_device(
    session: AsyncSession, device_id: str = DEVICE_ID, **overrides: object
) -> Device:
    values: dict[str, object] = {
        "device_id": device_id,
        "platform_type": "esp32c6",
        "link_type": "wifi",
        "power_class": "always_on",
    }
    values.update(overrides)
    device = Device(**values)
    session.add(device)
    await session.flush()
    return device


async def publish(
    session: AsyncSession,
    channel: str,
    body: object = None,
    *,
    device_id: str = DEVICE_ID,
    retained: bool = False,
    at: dt.datetime = NOW,
    raw: bytes | None = None,
    topic: str | None = None,
) -> DeviceEvent | None:
    """Drive one message through the handler with sensible defaults."""
    payload = raw if raw is not None else json.dumps(body).encode()
    return await handle_up_message(
        session,
        topic=topic if topic is not None else f"ff/v1/d/{device_id}/up/{channel}",
        payload=payload,
        retained=retained,
        received_at=at,
        tolerance=TOLERANCE,
    )


async def reload(session: AsyncSession, device_id: str = DEVICE_ID) -> Device:
    device = await session.scalar(select(Device).where(Device.device_id == device_id))
    assert device is not None
    await session.refresh(device)
    return device


# --- announce ---------------------------------------------------------------


async def test_announce_updates_identity(session: AsyncSession) -> None:
    await seed_device(session, presence_reported=True)

    event = await publish(session, "announce", ANNOUNCE)

    device = await reload(session)
    assert device.fw_version == "1.4.2"
    assert device.agent_version == "0.3.0"
    assert device.platform_type == "esp32c6"
    assert device.link_type == "wifi"
    assert device.partition_layout == "ab-4m-v1"
    assert device.ota_slot_size == 1966080
    assert device.capabilities == ["ota", "selftest"]
    assert device.proto == 1
    assert device.last_seen == NOW
    assert event is not None
    assert event.type is EventType.DEVICE_ANNOUNCE
    assert event.device_id == DEVICE_ID
    assert event.online is True
    assert event.fw_version == "1.4.2"
    assert event.v == 1


async def test_announce_says_nothing_about_presence(session: AsyncSession) -> None:
    """`presence_reported` is the `up/presence` topic's business, and only its."""
    await seed_device(session)

    await publish(session, "announce", ANNOUNCE)

    device = await reload(session)
    assert device.presence_reported is None


async def test_announce_from_unknown_device_creates_nothing(session: AsyncSession) -> None:
    """The enrollment boundary: publishing to the broker must never join the fleet."""
    before = await session.scalar(select(func.count()).select_from(Device))

    event = await publish(session, "announce", ANNOUNCE, device_id="ffffffffffff")

    assert event is None
    assert await session.scalar(select(func.count()).select_from(Device)) == before


async def test_announce_for_decommissioned_device_is_dropped(session: AsyncSession) -> None:
    """A soft-deleted board must not resurrect itself from a retained topic."""
    await seed_device(session, decommissioned_at=NOW - dt.timedelta(days=1))

    event = await publish(session, "announce", ANNOUNCE)

    assert event is None
    device = await reload(session)
    assert device.fw_version is None
    assert device.last_seen is None


async def test_payload_device_id_must_match_the_topic(session: AsyncSession) -> None:
    """Impersonation: the topic is what the `%u` pattern ACL binds to the username."""
    await seed_device(session)

    event = await publish(session, "announce", {**ANNOUNCE, "device_id": "b0b1b2b3b4b5"})

    assert event is None
    device = await reload(session)
    assert device.fw_version is None
    assert device.last_seen is None


async def test_unknown_power_class_loses_only_that_field(session: AsyncSession) -> None:
    """`fw_version` is what says the OTA landed; do not lose it to a CHECK violation."""
    await seed_device(session)

    event = await publish(session, "announce", {**ANNOUNCE, "power_class": "hibernating"})

    device = await reload(session)
    assert device.fw_version == "1.4.2"
    assert device.power_class == "always_on"
    assert event is not None


async def test_power_class_and_wake_interval_are_written_as_a_pair(session: AsyncSession) -> None:
    await seed_device(session)

    await publish(
        session,
        "announce",
        {**ANNOUNCE, "power_class": "sleepy", "expected_wake_interval_s": 600},
    )

    device = await reload(session)
    assert device.power_class == "sleepy"
    assert device.expected_wake_interval_s == 600


async def test_sleepy_without_an_interval_is_refused_as_a_pair(session: AsyncSession) -> None:
    """The `sleepy_wake_interval` CHECK would take the whole announce down with it."""
    await seed_device(session)

    event = await publish(session, "announce", {**ANNOUNCE, "power_class": "sleepy"})

    device = await reload(session)
    assert device.power_class == "always_on"
    assert device.expected_wake_interval_s is None
    assert device.fw_version == "1.4.2", "the rest of the announce must still land"
    assert event is not None


async def test_unknown_link_type_is_written_through(session: AsyncSession) -> None:
    """No PG enums: a link nobody has invented yet must still show in the dashboard."""
    await seed_device(session)

    await publish(session, "announce", {**ANNOUNCE, "link_type": "satellite"})

    assert (await reload(session)).link_type == "satellite"


# --- presence ---------------------------------------------------------------


async def test_presence_true_then_false_sets_reported_presence(session: AsyncSession) -> None:
    await seed_device(session)

    up = await publish(session, "presence", {"online": True})
    assert (await reload(session)).presence_reported is True
    assert up is not None and up.online is True and up.type is EventType.DEVICE_PRESENCE

    down = await publish(session, "presence", {"online": False})
    assert (await reload(session)).presence_reported is False
    assert down is not None and down.online is False


@pytest.mark.parametrize(
    ("power_class", "extra"),
    [("always_on", {}), ("sleepy", {"expected_wake_interval_s": 600})],
)
async def test_offline_presence_never_advances_last_seen(
    session: AsyncSession, power_class: str, extra: dict[str, object]
) -> None:
    """The LWT is published by the broker, not the board.

    For a `sleepy` device this is the difference between a bricked e-paper frame
    reading dead and reading alive forever: its LWT fires on every normal sleep.
    """
    seen = NOW - dt.timedelta(hours=3)
    await seed_device(session, power_class=power_class, last_seen=seen, **extra)

    await publish(session, "presence", {"online": False})

    device = await reload(session)
    assert device.last_seen == seen
    assert device.presence_reported is False


async def test_presence_without_online_is_dropped(session: AsyncSession) -> None:
    await seed_device(session)

    assert await publish(session, "presence", {"note": "hello"}) is None
    assert (await reload(session)).presence_reported is None


# --- retained replay --------------------------------------------------------


async def test_retained_replay_applies_state_but_not_liveness(session: AsyncSession) -> None:
    """An ingestor restart replays the whole retained set — for the dead fleet too."""
    await seed_device(session)

    await publish(session, "announce", ANNOUNCE, retained=True)
    device = await reload(session)
    assert device.fw_version == "1.4.2"
    assert device.last_seen is None

    await publish(session, "presence", {"online": True}, retained=True)
    device = await reload(session)
    assert device.presence_reported is True
    assert device.last_seen is None


async def test_retained_replay_on_another_channel_is_ignored(session: AsyncSession) -> None:
    """`up/status` is retained too; a replay of it proves nothing about liveness."""
    await seed_device(session)

    assert await publish(session, "status", {"state": "confirmed"}, retained=True) is None
    assert (await reload(session)).last_seen is None


# --- heartbeat and last_seen ------------------------------------------------


async def test_heartbeat_advances_last_seen_and_fw_version(session: AsyncSession) -> None:
    await seed_device(session, presence_reported=True)

    event = await publish(session, "hb", {"fw_version": "1.4.3", "uptime_s": 81234, "rssi": -61})

    device = await reload(session)
    assert device.last_seen == NOW
    assert device.fw_version == "1.4.3"
    assert event is not None
    assert event.type is EventType.DEVICE_HEARTBEAT
    assert event.online is True


async def test_last_seen_is_monotonic(session: AsyncSession) -> None:
    """QoS 1 is at-least-once: a re-delivery must not move a device backwards."""
    await seed_device(session)

    await publish(session, "hb", {}, at=NOW)
    await publish(session, "hb", {}, at=NOW - dt.timedelta(hours=1))

    assert (await reload(session)).last_seen == NOW


@pytest.mark.parametrize("channel", ["status", "telemetry", "log", "some-future-channel"])
async def test_other_channels_only_prove_liveness(session: AsyncSession, channel: str) -> None:
    """R1 owns `deploy_events`; R3 owns telemetry. At R0 they move `last_seen` only."""
    await seed_device(session)

    event = await publish(session, channel, {"state": "confirmed", "anything": 1})

    assert event is not None
    assert event.type is EventType.DEVICE_SEEN
    assert (await reload(session)).last_seen == NOW
    assert await session.scalar(select(func.count()).select_from(DeployEvent)) == 0


async def test_sleepy_device_online_is_derived_from_last_seen(session: AsyncSession) -> None:
    """The event's `online` comes from the post-write row, not from the payload."""
    await seed_device(session, power_class="sleepy", expected_wake_interval_s=600)

    event = await publish(session, "hb", {})

    assert event is not None
    assert event.online is True


# --- garbage in ---------------------------------------------------------------


@pytest.mark.parametrize(
    "raw",
    [
        b"not json at all",
        b"\xff\xfe\x00",
        b"[1, 2, 3]",
        b'{"online": "yes please"}',
        b'{"fw_version": 12}',
    ],
)
async def test_undecodable_payloads_are_dropped_quietly(session: AsyncSession, raw: bytes) -> None:
    await seed_device(session)

    assert await publish(session, "presence", raw=raw) is None
    assert (await reload(session)).last_seen is None


async def test_empty_payload_is_the_retained_clear_and_is_silent(session: AsyncSession) -> None:
    """Decommissioning clears retained topics with a zero-length publish."""
    await seed_device(session)

    assert await publish(session, "presence", raw=b"", retained=True) is None
    assert await publish(session, "announce", raw=b"") is None
    assert (await reload(session)).last_seen is None


@pytest.mark.parametrize(
    "topic",
    [
        "ff/v1/d/A4CF12B3DE90/up/hb",  # uppercase MAC — the CHECK's format rule
        "ff/v1/d/xyz/up/hb",
        "ff/v2/d/a4cf12b3de90/up/hb",
        "ff/v1/d/a4cf12b3de90/dn/cmd",
        "ff/v1/d/a4cf12b3de90/up",
        "nonsense",
        "",
    ],
)
async def test_bad_topics_write_nothing(session: AsyncSession, topic: str) -> None:
    await seed_device(session)

    assert await publish(session, "hb", {}, topic=topic) is None
    assert (await reload(session)).last_seen is None


# --- the NOTIFY itself --------------------------------------------------------


@pytest.fixture
async def listener() -> AsyncIterator[asyncpg.Connection]:
    """A raw asyncpg connection LISTENing on `ff_events`.

    `LISTEN` cannot share a pooled connection, which is also how R0-be-5 will have to
    do it: one dedicated connection per API worker.
    """
    dsn = asyncpg_dsn(database_url_for(TEST_DB_NAME))
    connection = await asyncpg.connect(dsn)
    try:
        yield connection
    finally:
        await connection.close()


async def test_notify_is_delivered_on_commit_and_never_on_rollback(
    engine: AsyncEngine, listener: asyncpg.Connection
) -> None:
    """The property R0-be-5 depends on: transactional delivery, real committed rows.

    The rolled-back `session` fixture would hide it entirely, so this test commits and
    cleans up after itself.
    """
    device_id = "dddddddddd10"
    received: asyncio.Queue[str] = asyncio.Queue()
    await listener.add_listener(EVENTS_CHANNEL, lambda *args: received.put_nowait(args[-1]))

    try:
        async with engine.begin() as setup:
            await setup.execute(
                text(
                    "INSERT INTO devices (device_id, platform_type, link_type, power_class) "
                    "VALUES (:d, 'esp32c6', 'wifi', 'always_on')"
                ),
                {"d": device_id},
            )

        # A rolled-back ingest must announce nothing: SSE can never report a row the
        # database does not have.
        async with AsyncSession(engine) as rolled_back:
            await publish(rolled_back, "hb", {"fw_version": "9.9.9"}, device_id=device_id)
            await rolled_back.rollback()

        async with AsyncSession(engine) as committed:
            event = await publish(committed, "hb", {"fw_version": "1.4.2"}, device_id=device_id)
            await committed.commit()
        assert event is not None

        payload = json.loads(await asyncio.wait_for(received.get(), timeout=5))
        assert payload["v"] == 1
        assert payload["type"] == "device.heartbeat"
        assert payload["device_id"] == device_id
        assert payload["fw_version"] == "1.4.2"
        assert payload["online"] is False, "no presence message yet, so always_on is offline"
        assert received.empty(), "the rolled-back transaction must have emitted nothing"

        # The envelope R0-be-5 parses is the one this module ships.
        assert DeviceEvent.model_validate(payload).device_id == device_id
    finally:
        # The listener connection is closed by its fixture; only the committed row
        # needs undoing, since this test does not get the rollback fixture.
        async with engine.begin() as cleanup:
            await cleanup.execute(
                text("DELETE FROM devices WHERE device_id = :d"), {"d": device_id}
            )

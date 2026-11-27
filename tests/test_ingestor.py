"""Core MQTT ingestor path, readiness capability summary, and per-device rate budget (`A-15`, `T-10`).

`up/status`, `up/ack`, `reset_reason`, and deploy KPI accounting tests live in
`tests/test_ingestor_telemetry_and_logs.py`.
"""

from __future__ import annotations

import asyncio
import datetime as dt
import json
import logging
from collections.abc import AsyncIterator
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock, patch

import asyncpg
import pytest
from sqlalchemy import func, select, text
from sqlalchemy.ext.asyncio import AsyncEngine, AsyncSession, async_sessionmaker

from fleetforge.db.base import asyncpg_dsn
from fleetforge.db.models import DeployEvent, Device
from fleetforge.events import EVENTS_CHANNEL, DeviceEvent, EventType
from fleetforge.ingest.handlers import handle_up_message
from fleetforge.ingest.rate_budget import DeviceIngestRateLimiter
from fleetforge.ingestor import main as ingestor_main
from fleetforge.ingestor.main import (
    handle_message,
    ingestor_capabilities_status,
    log_ingestor_readiness,
    touch_heartbeat,
)
from fleetforge.ingestor.protocol import (
    AnnouncePayload,
    HeartbeatPayload,
    PresencePayload,
    StatusPayload,
    decode,
)
from tests.conftest import TEST_DB_NAME, capture_logs, database_url_for, settings_for_tests
from tests.support_ingestor import (
    ANNOUNCE as ANNOUNCE,
)
from tests.support_ingestor import (
    DEVICE_ID as DEVICE_ID,
)
from tests.support_ingestor import (
    NOW as NOW,
)
from tests.support_ingestor import (
    TOLERANCE as TOLERANCE,
)
from tests.support_ingestor import (
    publish as publish,
)
from tests.support_ingestor import (
    reload as reload,
)
from tests.support_ingestor import (
    seed_device as seed_device,
)


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


@pytest.mark.parametrize("reported", [None, False], ids=["after-enroll", "after-lwt"])
async def test_a_live_announce_never_reports_a_connecting_board_offline(
    session: AsyncSession, reported: bool | None
) -> None:
    """S0-bug-2: `presence_reported` is the previous session's, so the snapshot is `null`."""
    await seed_device(session, presence_reported=reported)

    event = await publish(session, "announce", ANNOUNCE)

    assert event is not None
    assert event.online is None, "a live announce must say 'not known yet', never False"
    assert (await reload(session)).presence_reported is reported

    event = await publish(session, "presence", {"online": True})

    assert event is not None
    assert event.online is True


async def test_a_retained_announce_replay_still_reads_offline(session: AsyncSession) -> None:
    """An ingestor restart must not make a dead fleet look 'unknown'."""
    await seed_device(session, presence_reported=False)

    event = await publish(session, "announce", ANNOUNCE, retained=True)

    assert event is not None
    assert event.online is False


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


async def test_announce_stores_the_network_the_board_is_on(session: AsyncSession) -> None:
    await seed_device(session)

    await publish(session, "announce", {**ANNOUNCE, "ssid": "shed", "known_networks": 2})

    device = await reload(session)
    assert device.ssid == "shed"
    assert device.known_networks == 2


async def test_the_network_is_written_as_a_pair_on_every_announce(session: AsyncSession) -> None:
    """Absent means 'not reported', not 'no change'."""
    await seed_device(session)

    await publish(session, "announce", {**ANNOUNCE, "ssid": "shed", "known_networks": 2})
    await publish(session, "announce", {**ANNOUNCE, "ssid": "home", "known_networks": 3})
    device = await reload(session)
    assert (device.ssid, device.known_networks) == ("home", 3)

    await publish(session, "announce", ANNOUNCE)
    device = await reload(session)
    assert (device.ssid, device.known_networks) == (None, None)
    assert device.fw_version == "1.4.2"


async def test_ethernet_reports_no_network(session: AsyncSession) -> None:
    await seed_device(session, ssid="shed", known_networks=2)

    await publish(
        session,
        "announce",
        {**ANNOUNCE, "link_type": "ethernet", "ssid": None, "known_networks": None},
    )

    device = await reload(session)
    assert device.link_type == "ethernet"
    assert (device.ssid, device.known_networks) == (None, None)


@pytest.mark.parametrize(
    ("ssid", "known_networks"),
    [(42, True), ("a" * 33, -1), (["shed"], 2.0), ("", "2")],
)
async def test_a_malformed_network_never_loses_the_announce(
    session: AsyncSession, ssid: object, known_networks: object
) -> None:
    """A `ValidationError` would drop the `fw_version` that says the OTA landed."""
    await seed_device(session, ssid="shed", known_networks=2)

    event = await publish(
        session,
        "announce",
        {**ANNOUNCE, "fw_version": "9.9.9", "ssid": ssid, "known_networks": known_networks},
    )

    assert event is not None
    device = await reload(session)
    assert device.fw_version == "9.9.9"
    assert (device.ssid, device.known_networks) == (None, None)


@pytest.mark.parametrize(
    "raw_ssid",
    [
        b'"a\\u0000b"',
        b'"\\ud800"',
        b'"tab\\there"',
    ],
)
async def test_an_ssid_the_database_would_refuse_is_stored_as_null(
    session: AsyncSession, raw_ssid: bytes
) -> None:
    await seed_device(session)
    raw = (
        b'{"proto":1,"device_id":"' + DEVICE_ID.encode() + b'","fw_version":"9.9.10",'
        b'"link_type":"wifi","power_class":"always_on","ssid":' + raw_ssid + b","
        b'"known_networks":1}'
    )

    event = await publish(session, "announce", raw=raw)

    assert event is not None
    device = await reload(session)
    assert device.fw_version == "9.9.10"
    assert device.ssid is None
    assert device.known_networks == 1


async def test_a_malformed_network_is_logged_without_its_value(session: AsyncSession) -> None:
    await seed_device(session)
    ssid = "this-ssid-is-thirty-three-bytes!!"
    assert len(ssid) == 33

    with capture_logs() as records:
        await publish(session, "announce", {**ANNOUNCE, "ssid": ssid, "known_networks": -1})

    messages = [record.getMessage() for record in records if "unusable" in record.getMessage()]
    assert len(messages) == 2, messages
    assert any("ssid" in m and "33 bytes > 32" in m and DEVICE_ID in m for m in messages)
    assert any("known_networks" in m and "out of range" in m for m in messages)
    assert all(record.levelno == logging.INFO for record in records if "unusable" in record.msg)
    assert not any(ssid in record.getMessage() for record in records)


async def test_a_retained_announce_also_writes_the_network(session: AsyncSession) -> None:
    """The retained announce is the board's latest identity; only liveness is withheld."""
    await seed_device(session, ssid="old", known_networks=4)

    await publish(
        session, "announce", {**ANNOUNCE, "ssid": "shed", "known_networks": 2}, retained=True
    )

    device = await reload(session)
    assert (device.ssid, device.known_networks) == ("shed", 2)
    assert device.last_seen is None


SHA = "1fa67e6bbd034e434d04e9d6f4f52bbe899361602cd498573eb3bde97d1559ed"
MEASURED = {"flash_chip_size": 4194304, "partition_table_sha256": SHA, "rollback_capable": True}


def measurements(device: Device) -> tuple[int | None, str | None, bool | None]:
    return (device.flash_chip_size, device.partition_table_sha256, device.rollback_capable)


async def test_announce_stores_the_board_measurements(session: AsyncSession) -> None:
    await seed_device(session)

    await publish(session, "announce", {**ANNOUNCE, **MEASURED})

    assert measurements(await reload(session)) == (4194304, SHA, True)


async def test_the_measurements_are_written_as_a_triple_on_every_announce(
    session: AsyncSession,
) -> None:
    """Absent means 'unknown', not 'no change'."""
    await seed_device(session)

    await publish(session, "announce", {**ANNOUNCE, **MEASURED})
    await publish(session, "announce", ANNOUNCE)

    device = await reload(session)
    assert measurements(device) == (None, None, None)
    assert device.fw_version == "1.4.2"


async def test_rollback_capable_false_is_a_value_not_an_absence(session: AsyncSession) -> None:
    await seed_device(session, rollback_capable=True)

    await publish(session, "announce", {**ANNOUNCE, "rollback_capable": False})

    device = await reload(session)
    assert device.rollback_capable is False
    assert (device.flash_chip_size, device.partition_table_sha256) == (None, None)


@pytest.mark.parametrize(
    "malformed",
    [
        {"flash_chip_size": "4MB", "partition_table_sha256": SHA.upper(), "rollback_capable": 1},
        {"flash_chip_size": 0, "partition_table_sha256": SHA[:63], "rollback_capable": "true"},
        {"flash_chip_size": 4194304.0, "partition_table_sha256": 123, "rollback_capable": []},
        {"flash_chip_size": True, "partition_table_sha256": "", "rollback_capable": "false"},
    ],
)
async def test_malformed_measurements_never_lose_the_announce(
    session: AsyncSession, malformed: dict[str, object]
) -> None:
    """A `ValidationError` would drop the `fw_version` that says the OTA landed."""
    await seed_device(session, flash_chip_size=4194304, partition_table_sha256=SHA)

    event = await publish(session, "announce", {**ANNOUNCE, "fw_version": "9.9.9", **malformed})

    assert event is not None
    device = await reload(session)
    assert device.fw_version == "9.9.9"
    assert measurements(device) == (None, None, None)


async def test_measurements_the_database_would_refuse_are_stored_as_null(
    session: AsyncSession,
) -> None:
    await seed_device(session)
    raw = (
        b'{"proto":1,"device_id":"' + DEVICE_ID.encode() + b'","fw_version":"9.9.11",'
        b'"link_type":"wifi","power_class":"always_on",'
        b'"flash_chip_size":18446744073709551616,'
        b'"partition_table_sha256":"' + SHA[:63].encode() + b'\\u0000",'
        b'"rollback_capable":false}'
    )

    event = await publish(session, "announce", raw=raw)

    assert event is not None
    device = await reload(session)
    assert device.fw_version == "9.9.11"
    assert measurements(device) == (None, None, False)


async def test_malformed_measurements_are_logged_without_their_values(
    session: AsyncSession,
) -> None:
    await seed_device(session)
    with capture_logs() as records:
        await publish(
            session,
            "announce",
            {
                **ANNOUNCE,
                "flash_chip_size": 2**64,
                "partition_table_sha256": SHA.upper(),
                "rollback_capable": "true",
            },
        )

    unusable = [record for record in records if "unusable" in record.getMessage()]
    messages = [record.getMessage() for record in unusable]
    assert len(messages) == 3, messages
    expected = f"device {DEVICE_ID} announced an unusable flash_chip_size (out of range)"
    assert any(expected in m for m in messages)
    assert any("partition_table_sha256 (not 64 lowercase hex characters)" in m for m in messages)
    assert any("rollback_capable (not a boolean)" in m for m in messages)
    assert all(record.levelno == logging.INFO for record in unusable)
    everything = " ".join(record.getMessage() for record in records)
    assert str(2**64) not in everything
    assert SHA.upper() not in everything


async def test_a_retained_announce_also_writes_the_measurements(session: AsyncSession) -> None:
    await seed_device(session, flash_chip_size=8388608, rollback_capable=False)

    await publish(session, "announce", {**ANNOUNCE, **MEASURED}, retained=True)

    device = await reload(session)
    assert measurements(device) == (4194304, SHA, True)
    assert device.last_seen is None


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
    """The LWT is published by the broker, not the board."""
    seen = NOW - dt.timedelta(hours=3)
    await seed_device(
        session, device_id=DEVICE_ID, power_class=power_class, last_seen=seen, **extra
    )

    await publish(session, "presence", {"online": False})

    device = await reload(session)
    assert device.last_seen == seen
    assert device.presence_reported is False


async def test_presence_without_online_is_dropped(session: AsyncSession) -> None:
    await seed_device(session)

    assert await publish(session, "presence", {"note": "hello"}) is None
    assert (await reload(session)).presence_reported is None


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


@pytest.mark.parametrize("channel", ["telemetry", "log", "some-future-channel"])
async def test_retained_replay_on_another_channel_is_ignored(
    session: AsyncSession, channel: str
) -> None:
    """A channel that only proves liveness has nothing to say when it is a replay."""
    await seed_device(session)

    assert await publish(session, channel, {"anything": 1}, retained=True) is None
    assert (await reload(session)).last_seen is None


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


@pytest.mark.parametrize("channel", ["telemetry", "log", "some-future-channel"])
async def test_other_channels_only_prove_liveness(session: AsyncSession, channel: str) -> None:
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
        "ff/v1/d/A4CF12B3DE90/up/hb",
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


@pytest.fixture
async def listener() -> AsyncIterator[asyncpg.Connection]:
    """A raw asyncpg connection LISTENing on `ff_events`."""
    dsn = asyncpg_dsn(database_url_for(TEST_DB_NAME))
    connection = await asyncpg.connect(dsn)
    try:
        yield connection
    finally:
        await connection.close()


async def test_notify_is_delivered_on_commit_and_never_on_rollback(
    engine: AsyncEngine, listener: asyncpg.Connection
) -> None:
    """Transactional delivery on `ff_events`: committed rows notify, rolled-back rows do not."""
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
        assert payload["online"] is False
        assert received.empty(), "the rolled-back transaction must have emitted nothing"
        assert DeviceEvent.model_validate(payload).device_id == device_id
    finally:
        async with engine.begin() as cleanup:
            await cleanup.execute(
                text("DELETE FROM devices WHERE device_id = :d"), {"d": device_id}
            )


async def test_announce_safe_mode_is_stored_and_cleared_when_absent(
    session: AsyncSession,
) -> None:
    """D-3: `up/announce.safe_mode` persists on `devices.safe_mode` and clears when absent."""
    await seed_device(session)

    await publish(
        session,
        "announce",
        {
            "device_id": DEVICE_ID,
            "platform_type": "esp32",
            "fw_version": "1.4.0",
            "safe_mode": True,
        },
    )
    dev = await session.get(Device, DEVICE_ID)
    assert dev is not None
    assert dev.safe_mode is True

    await publish(
        session,
        "announce",
        {
            "device_id": DEVICE_ID,
            "platform_type": "esp32",
            "fw_version": "1.4.1",
            "safe_mode": "true",
        },
    )
    await session.refresh(dev)
    assert dev.fw_version == "1.4.1"
    assert dev.safe_mode is False

    await publish(
        session,
        "announce",
        {
            "device_id": DEVICE_ID,
            "platform_type": "esp32",
            "fw_version": "1.4.1",
            "safe_mode": True,
        },
    )
    await session.refresh(dev)
    assert dev.safe_mode is True

    await publish(
        session,
        "announce",
        {
            "device_id": DEVICE_ID,
            "platform_type": "esp32",
            "fw_version": "1.4.2",
        },
    )
    await session.refresh(dev)
    assert dev.fw_version == "1.4.2"
    assert dev.safe_mode is False


async def test_deeply_nested_payload_is_dropped_not_raised(
    engine: AsyncEngine, session: AsyncSession
) -> None:
    """A-1: 20 KB or deeply nested retained payloads return None and never exit `handle_message`."""
    await seed_device(session)
    await session.commit()
    sm = async_sessionmaker(engine, expire_on_commit=False)

    channels_and_models = (
        ("announce", AnnouncePayload),
        ("status", StatusPayload),
        ("presence", PresencePayload),
        ("hb", HeartbeatPayload),
    )
    for raw_payload in (b"[" * 20000, b"[" * 2000, b"{" + b'"a":{' * 1500 + b"}" * 1501):
        for channel, model in channels_and_models:
            topic = f"ff/v1/d/{DEVICE_ID}/up/{channel}"
            assert decode(model, topic, raw_payload) is None
            fake_msg = SimpleNamespace(
                topic=SimpleNamespace(value=topic),
                payload=raw_payload,
                qos=1,
                retain=True,
            )
            await handle_message(fake_msg, sm, 2.5)  # type: ignore[arg-type]

    overlong_status = json.dumps({"cmd_id": "x" * 500, "state": "y" * 500}).encode("utf-8")
    parsed = decode(StatusPayload, f"ff/v1/d/{DEVICE_ID}/up/status", overlong_status)
    assert parsed is not None
    assert parsed.cmd_id is None
    assert parsed.state is None


def test_ingestor_readiness_lists_disabled_optional_capabilities(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A-15: startup readiness reports disabled optional capabilities and writes JSON heartbeat."""
    minimal = settings_for_tests(
        s3_endpoint_url=None,
        s3_bucket=None,
        gcs_bucket=None,
        otel_exporter_otlp_endpoint=None,
        ingest_device_rate_per_min=120,
        ingest_device_burst=30,
    )
    with capture_logs("fleetforge.ingestor") as records:
        status = log_ingestor_readiness(minimal)

    assert status["status"] == "alive"
    assert status["enabled_capabilities"] == [
        "mqtt_up_ingest",
        "retention_sweep",
        "device_rate_budget",
    ]
    assert status["disabled_capabilities"] == [
        "object_store_orphan_gc",
        "global_otlp_export",
    ]
    assert status["rate_budget"] == {"rate_per_min": 120, "burst": 30}
    assert any(
        "ingestor readiness enabled=mqtt_up_ingest,retention_sweep,device_rate_budget "
        "disabled=object_store_orphan_gc,global_otlp_export" in r.getMessage()
        for r in records
    )

    hb_file = tmp_path / "ingestor-alive"
    monkeypatch.setattr(ingestor_main, "HEARTBEAT_PATH", hb_file)
    touch_heartbeat(status)
    assert hb_file.is_file()
    assert json.loads(hb_file.read_text(encoding="utf-8")) == status

    full = settings_for_tests(
        s3_endpoint_url="http://minio:9000",
        s3_bucket="fleetforge",
        otel_exporter_otlp_endpoint="https://otel.example.com",
        ingest_device_rate_per_min=0,
        ingest_device_burst=30,
    )
    full_status = ingestor_capabilities_status(full)
    assert full_status["disabled_capabilities"] == ["device_rate_budget"]
    assert "object_store_orphan_gc" in full_status["enabled_capabilities"]
    assert "global_otlp_export" in full_status["enabled_capabilities"]


async def test_per_device_rate_budget_throttles_noisy_logs_without_dropping_status_or_other_devices() -> (
    None
):
    """A-15: per-device token bucket drops excess `log`/`telemetry`/`hb` before PostgreSQL while
    never dropping `status`/`announce`/`ack`/`presence` or starving sibling devices."""
    clock = [100.0]
    limiter = DeviceIngestRateLimiter(
        rate_per_min=60,
        burst=3,
        max_devices=4,
        warn_interval_s=30.0,
        clock=lambda: clock[0],
    )
    noisy_id = "a4cf12b3de90"
    quiet_id = "a4cf12b3de91"
    session = AsyncMock()

    dispatched: list[tuple[str, str]] = []

    async def fake_dispatch(
        _session: object,
        *,
        channel: str,
        device_id: str,
        **_kwargs: object,
    ) -> tuple[None, EventType, None, bool]:
        dispatched.append((device_id, channel))
        return None, EventType.DEVICE_SEEN, None, True

    with (
        patch("fleetforge.ingest.handlers._dispatch_primary_channel", side_effect=fake_dispatch),
        capture_logs("fleetforge.ingest.rate_budget") as warn_records,
    ):
        for i in range(8):
            await handle_up_message(
                session,
                topic=f"ff/v1/d/{noisy_id}/up/log",
                payload=json.dumps({"lines": [{"t": i, "lvl": "I", "msg": "spam"}]}).encode(),
                retained=False,
                received_at=NOW,
                tolerance=TOLERANCE,
                rate_limiter=limiter,
            )
        for ch, body in (
            ("telemetry", {"samples": [{"t": 0, "k": "temp_c", "v": 22.0}]}),
            ("hb", {"fw_version": "1.4.2"}),
        ):
            await handle_up_message(
                session,
                topic=f"ff/v1/d/{noisy_id}/up/{ch}",
                payload=json.dumps(body).encode(),
                retained=False,
                received_at=NOW,
                tolerance=TOLERANCE,
                rate_limiter=limiter,
            )

        # Critical lifecycle/OTA channels on the throttled board MUST pass unimpeded!
        for crit_ch, crit_body in (
            ("status", {"cmd_id": "cmd-1", "state": "confirmed"}),
            ("announce", ANNOUNCE),
            ("ack", {"id": "c1", "type": "apply", "result": "accepted"}),
            ("presence", {"online": True}),
        ):
            await handle_up_message(
                session,
                topic=f"ff/v1/d/{noisy_id}/up/{crit_ch}",
                payload=json.dumps(crit_body).encode(),
                retained=False,
                received_at=NOW,
                tolerance=TOLERANCE,
                rate_limiter=limiter,
            )

        # Sibling board is unaffected by noisy_id's exhausted bucket.
        await handle_up_message(
            session,
            topic=f"ff/v1/d/{quiet_id}/up/log",
            payload=b'{"lines":[{"t":0,"lvl":"I","msg":"ok"}]}',
            retained=False,
            received_at=NOW,
            tolerance=TOLERANCE,
            rate_limiter=limiter,
        )

    assert dispatched == [
        (noisy_id, "log"),
        (noisy_id, "log"),
        (noisy_id, "log"),
        (noisy_id, "status"),
        (noisy_id, "announce"),
        (noisy_id, "ack"),
        (noisy_id, "presence"),
        (quiet_id, "log"),
    ]
    assert limiter.dropped_total == 7
    assert len(warn_records) == 1
    assert f"device {noisy_id} exhausted ingest rate budget" in warn_records[0].getMessage()

    # Refill after 2.0s (60/min = 1 token/s -> +2 tokens) and verify LRU bound (`max_devices=4`).
    clock[0] += 2.0
    assert limiter.allow(noisy_id, "hb") is True
    assert limiter.allow(noisy_id, "telemetry") is True
    assert limiter.allow(noisy_id, "log") is False
    for idx in range(10):
        assert limiter.allow(f"b4cf12b3de0{idx}", "hb") is True
    assert limiter.tracked_devices == 4

    disabled = DeviceIngestRateLimiter(rate_per_min=0, burst=30)
    assert disabled.enabled is False
    assert all(disabled.allow(noisy_id, "log") for _ in range(100))

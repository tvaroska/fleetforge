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
import logging
from collections.abc import AsyncIterator

import asyncpg
import pytest
from sqlalchemy import func, select, text
from sqlalchemy.ext.asyncio import AsyncEngine, AsyncSession

from fleetforge.db.base import asyncpg_dsn
from fleetforge.db.models import DeployEvent, Device
from fleetforge.deploys import record_requested
from fleetforge.events import EVENTS_CHANNEL, DeviceEvent, EventType
from fleetforge.ingestor.handlers import handle_up_message
from tests.conftest import TEST_DB_NAME, capture_logs, database_url_for

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


@pytest.mark.parametrize("channel", ["telemetry", "log", "some-future-channel"])
async def test_retained_replay_on_another_channel_is_ignored(
    session: AsyncSession, channel: str
) -> None:
    """A channel that only proves liveness has nothing to say when it is a replay.

    `up/status` is the exception and is ingested retained — see
    `test_a_retained_status_does_not_prove_liveness`.
    """
    await seed_device(session)

    assert await publish(session, channel, {"anything": 1}, retained=True) is None
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


@pytest.mark.parametrize("channel", ["telemetry", "log", "some-future-channel"])
async def test_other_channels_only_prove_liveness(session: AsyncSession, channel: str) -> None:
    """R4 owns telemetry and log; an unknown channel is a future agent's.

    `status` is deliberately **not** in this list any more: R1-be-4 made it the one
    channel that writes `deploy_events`.
    """
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


# --- up/status → deploy_events (R1-be-4) --------------------------------------

CMD_ID = "01J9Z0000000000000000DEPL0"


async def seed_intent(
    session: AsyncSession,
    *,
    device_id: str = DEVICE_ID,
    cmd_id: str = CMD_ID,
    artifact_version: str = "1.5.0",
    from_version: str | None = "1.4.2",
) -> None:
    """The server-authored `requested` row, written by the shipped writer.

    Built through `deploys.record_requested` rather than by hand so the tests exercise
    the same row shape the API produces — the version columns an observed row copies
    are only correct if both halves agree.
    """
    await record_requested(
        session,
        device_id=device_id,
        cmd_id=cmd_id,
        artifact_version=artifact_version,
        from_version=from_version,
        sha256="a" * 64,
        size_bytes=1966080,
        target="esp32c6",
        apply="immediate",
    )


async def deploy_rows(session: AsyncSession, cmd_id: str = CMD_ID) -> list[DeployEvent]:
    return list(
        (
            await session.scalars(
                select(DeployEvent).where(DeployEvent.cmd_id == cmd_id).order_by(DeployEvent.id)
            )
        ).all()
    )


async def test_the_whole_walk_is_recorded_as_transitions(session: AsyncSession) -> None:
    """Every state the board reports becomes a row, and none of them ends the transaction."""
    await seed_device(session)
    await seed_intent(session)

    walk = ["staging", "downloading", "verifying", "staged", "applying", "rebooting"]
    for pct, state in enumerate(walk):
        event = await publish(session, "status", {"cmd_id": CMD_ID, "state": state, "pct": pct})
        assert event is not None
        assert event.type is EventType.DEVICE_DEPLOY

    rows = await deploy_rows(session)
    assert [row.state for row in rows] == ["requested", *walk]
    assert [row.is_terminal for row in rows] == [False] * 7
    assert {row.artifact_version for row in rows} == {"1.5.0"}


async def test_confirmed_is_terminal_and_carries_the_intended_versions(
    session: AsyncSession,
) -> None:
    """Without the versions off the `requested` row, R6's delivery-success KPI is uncomputable."""
    await seed_device(session)
    await seed_intent(session)

    await publish(session, "status", {"cmd_id": CMD_ID, "state": "confirmed", "pct": 100})

    row = (await deploy_rows(session))[-1]
    assert row.state == "confirmed"
    assert row.is_terminal is True
    assert row.artifact_version == "1.5.0"
    assert row.from_version == "1.4.2"
    assert row.detail == {"pct": 100}
    assert row.at == NOW


@pytest.mark.parametrize("state", ["failed", "rolled_back"])
async def test_failure_and_rollback_are_terminal(session: AsyncSession, state: str) -> None:
    """A table that only records successes is a KPI that only measures them.

    `rolled_back` is a fleet-safety **save**, not a loss — it still ends the transaction.
    """
    await seed_device(session)
    await seed_intent(session)

    await publish(session, "status", {"cmd_id": CMD_ID, "state": state, "detail": "boom"})

    row = (await deploy_rows(session))[-1]
    assert row.state == state
    assert row.is_terminal is True
    assert row.detail == {"detail": "boom"}


async def test_a_retained_replay_writes_no_second_row(session: AsyncSession) -> None:
    """`up/status` is retained and the ingestor re-subscribes: without dedupe, every
    restart duplicates the KPI rows forever."""
    await seed_device(session)
    await seed_intent(session)
    body = {"cmd_id": CMD_ID, "state": "confirmed", "pct": 100}

    first = await publish(session, "status", body)
    replay = await publish(session, "status", body, retained=True)
    again = await publish(session, "status", body, retained=True)

    assert first is not None and first.type is EventType.DEVICE_DEPLOY
    assert replay is not None and replay.type is EventType.DEVICE_SEEN
    assert again is not None and again.type is EventType.DEVICE_SEEN
    assert [row.state for row in await deploy_rows(session)] == ["requested", "confirmed"]


async def test_a_retained_status_does_not_prove_liveness(session: AsyncSession) -> None:
    """The replay of a dead board's last status must not mark the fleet alive."""
    await seed_device(session)
    await seed_intent(session)

    await publish(session, "status", {"cmd_id": CMD_ID, "state": "staged"}, retained=True)
    assert (await reload(session)).last_seen is None
    assert len(await deploy_rows(session)) == 2, "the state is still recorded"

    await publish(session, "status", {"cmd_id": CMD_ID, "state": "applying"})
    assert (await reload(session)).last_seen == NOW


async def test_the_wire_may_not_author_the_servers_own_state(session: AsyncSession) -> None:
    """A board claiming `requested` is a firmware bug or a forgery. Never a row."""
    await seed_device(session)
    await seed_intent(session)

    with capture_logs() as records:
        event = await publish(session, "status", {"cmd_id": CMD_ID, "state": "requested"})

    assert [row.state for row in await deploy_rows(session)] == ["requested"]
    assert event is not None
    assert event.type is EventType.DEVICE_SEEN
    warnings = [r for r in records if r.levelno >= logging.WARNING]
    assert warnings, "the forgery attempt must be visible in the log"
    assert any("requested" in r.getMessage() for r in warnings)


async def test_status_from_an_unregistered_device_writes_nothing(session: AsyncSession) -> None:
    """`deploy_events.device_id` is an FK with `ON DELETE RESTRICT`: an insert here
    would raise `IntegrityError` and be swallowed as "ingest failed"."""
    before = await session.scalar(select(func.count()).select_from(DeployEvent))

    event = await publish(
        session,
        "status",
        {"cmd_id": CMD_ID, "state": "confirmed"},
        device_id="ffffffffffff",
    )

    assert event is None
    assert await session.scalar(select(func.count()).select_from(DeployEvent)) == before


async def test_status_from_a_decommissioned_device_writes_nothing(session: AsyncSession) -> None:
    await seed_device(session, decommissioned_at=NOW - dt.timedelta(days=1))
    before = await session.scalar(select(func.count()).select_from(DeployEvent))

    for retained in (False, True):
        assert (
            await publish(
                session, "status", {"cmd_id": CMD_ID, "state": "confirmed"}, retained=retained
            )
            is None
        )
    assert await session.scalar(select(func.count()).select_from(DeployEvent)) == before


async def test_a_status_with_no_cmd_id_only_proves_liveness(session: AsyncSession) -> None:
    """`idle` with no transaction would otherwise be a row per boot in a forever table."""
    await seed_device(session)

    event = await publish(session, "status", {"state": "idle"})

    assert event is not None
    assert event.type is EventType.DEVICE_SEEN
    assert (await reload(session)).last_seen == NOW
    assert await session.scalar(select(func.count()).select_from(DeployEvent)) == 0


async def test_an_unmapped_cmd_id_is_still_recorded(session: AsyncSession) -> None:
    """A transaction from before a database rebuild is an outcome too. Versions NULL."""
    await seed_device(session)

    event = await publish(session, "status", {"cmd_id": "unknown-cmd", "state": "confirmed"})

    assert event is not None
    assert event.type is EventType.DEVICE_DEPLOY
    row = (await deploy_rows(session, "unknown-cmd"))[0]
    assert row.is_terminal is True
    assert row.artifact_version is None
    assert row.from_version is None


async def test_an_unknown_state_is_recorded_and_is_not_terminal(session: AsyncSession) -> None:
    """The ingest path never rejects on vocabulary — but only the set ends a transaction."""
    await seed_device(session)
    await seed_intent(session)

    await publish(session, "status", {"cmd_id": CMD_ID, "state": "teleporting"})

    row = (await deploy_rows(session))[-1]
    assert row.state == "teleporting"
    assert row.is_terminal is False


@pytest.mark.parametrize("raw", [b"not json", b'{"state": 42}', b'{"cmd_id": []}'])
async def test_unusable_status_payloads_write_nothing(session: AsyncSession, raw: bytes) -> None:
    await seed_device(session)
    await seed_intent(session)

    await publish(session, "status", raw=raw)

    assert [row.state for row in await deploy_rows(session)] == ["requested"]


async def test_odd_but_usable_fields_are_coerced_not_dropped(session: AsyncSession) -> None:
    """A retained message that fails validation is an outcome lost on every reconnect."""
    await seed_device(session)
    await seed_intent(session)

    await publish(
        session,
        "status",
        {"cmd_id": CMD_ID, "state": "confirmed", "pct": "42", "detail": {"a": 1}, "extra": True},
    )

    row = (await deploy_rows(session))[-1]
    assert row.state == "confirmed"
    assert row.detail == {"detail": "{'a': 1}"}, "pct was not an int, the state still landed"


async def test_detail_is_sanitised_truncated_and_url_redacted(session: AsyncSession) -> None:
    """Device-controlled text in a table kept forever; the signed URL is a credential."""
    await seed_device(session)
    await seed_intent(session)

    await publish(
        session,
        "status",
        {
            "cmd_id": CMD_ID,
            "state": "failed",
            "detail": "get https://host/v1/artifact/ab/bin?sig=secret failed\nline two "
            + "x" * 300,
        },
    )

    detail = (await deploy_rows(session))[-1].detail
    assert isinstance(detail, dict)
    stored = detail["detail"]
    assert len(stored) == 200
    assert "http" not in stored
    assert "sig=secret" not in stored
    assert "<url>" in stored
    assert "\n" not in stored


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

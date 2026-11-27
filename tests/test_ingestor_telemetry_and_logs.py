"""Ingestor `up/status` -> `deploy_events`, `reset_reason`, `up/ack` -> `device_commands`, and KPI rules (`T-10`)."""

from __future__ import annotations

import datetime as dt
import json
import logging

import pytest
from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

from fleetforge import device_commands
from fleetforge.db.models import DeployEvent, Device, DeviceCommand
from fleetforge.deploys import DeploySender, counts_in_deploy_kpis, record_requested
from fleetforge.events import EventType
from fleetforge.ingestor.store import ANNOUNCE_FIELDS
from fleetforge.selftest_outcome import SelfTestOutcome
from tests.conftest import capture_logs
from tests.support_ingestor import (
    ANNOUNCE,
    DEVICE_ID,
    NOW,
    publish,
    reload,
    seed_device,
)

CMD_ID = "01J9Z0000000000000000DEPL0"
PRE_REBOOT_WALK = ["staging", "downloading", "verifying", "staged", "applying", "rebooting"]
COMMAND_ID = "c0ffee00c0ffee00c0ffee00c0ffee00"
OTHER_ID = "a4cf12b3de91"


async def seed_intent(
    session: AsyncSession,
    *,
    device_id: str = DEVICE_ID,
    cmd_id: str = CMD_ID,
    artifact_version: str = "1.5.0",
    from_version: str | None = "1.4.2",
) -> None:
    """The server-authored `requested` row, written by the shipped writer."""
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
        sent_by=DeploySender(subject="admin", token_id="t", credential=None),
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
        assert event.cmd_id == CMD_ID
        assert event.state == state
        assert event.is_terminal is False
        assert event.detail is None

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
    """`rolled_back` is a fleet-safety save, not a loss — it still ends the transaction."""
    await seed_device(session)
    await seed_intent(session)

    await publish(session, "status", {"cmd_id": CMD_ID, "state": state, "detail": "boom"})

    row = (await deploy_rows(session))[-1]
    assert row.state == state
    assert row.is_terminal is True
    assert row.detail == {"detail": "boom"}


async def test_a_retained_replay_writes_no_second_row(session: AsyncSession) -> None:
    """`up/status` is retained and the ingestor re-subscribes: dedupe prevents duplicate KPI rows."""
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


async def test_the_confirm_walk_ends_in_one_terminal_row(session: AsyncSession) -> None:
    await seed_device(session)
    await seed_intent(session)

    for state in [*PRE_REBOOT_WALK, "confirming"]:
        await publish(session, "status", {"cmd_id": CMD_ID, "state": state, "pct": None})
    await publish(session, "status", {"cmd_id": CMD_ID, "state": "confirmed", "pct": 100})

    rows = await deploy_rows(session)
    assert [row.state for row in rows] == ["requested", *PRE_REBOOT_WALK, "confirming", "confirmed"]
    assert [row.state for row in rows if row.is_terminal] == ["confirmed"]
    for row in rows[-2:]:
        assert (row.artifact_version, row.from_version) == ("1.5.0", "1.4.2")


async def test_the_rollback_walk_ends_in_one_terminal_row(session: AsyncSession) -> None:
    """`rolling_back` comes from the bad image, `rolled_back` from the one it returned to."""
    await seed_device(session)
    await seed_intent(session)
    detail = "returned to ota_0; ota_1 did not confirm"

    for state in [*PRE_REBOOT_WALK, "confirming", "rolling_back"]:
        await publish(session, "status", {"cmd_id": CMD_ID, "state": state})
    await publish(session, "status", {"cmd_id": CMD_ID, "state": "rolled_back", "detail": detail})

    rows = await deploy_rows(session)
    assert [row.state for row in rows][-3:] == ["confirming", "rolling_back", "rolled_back"]
    assert [row.state for row in rows if row.is_terminal] == ["rolled_back"]
    rolled = rows[-1]
    assert (rolled.artifact_version, rolled.from_version) == ("1.5.0", "1.4.2")
    assert rolled.detail == {"detail": detail}
    assert rows[-2].artifact_version == "1.5.0"


async def test_the_old_images_retained_rolled_back_is_recorded_once(session: AsyncSession) -> None:
    """One `rolled_back` row across republishes and retained broker replays."""
    await seed_device(session)
    await seed_intent(session)
    body = {"cmd_id": CMD_ID, "state": "rolled_back", "detail": "returned to ota_0"}

    await publish(session, "status", body)
    await publish(session, "status", body)
    replay = await publish(session, "status", body, retained=True)

    assert replay is not None and replay.type is EventType.DEVICE_SEEN
    assert [row.state for row in await deploy_rows(session)] == ["requested", "rolled_back"]


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

    event = await publish(
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
    assert event is not None
    assert event.detail == stored
    assert "sig=secret" not in event.model_dump_json()
    assert "host/v1" not in event.model_dump_json()


async def test_a_deploy_event_carries_the_transaction_and_a_terminal_detail(
    session: AsyncSession,
) -> None:
    """R4-be-2: `detail` only on a terminal row; non-terminal chatter never rides the frame."""
    await seed_device(session)
    await seed_intent(session)

    walking = await publish(
        session, "status", {"cmd_id": CMD_ID, "state": "downloading", "detail": "chunk 3"}
    )
    failed = await publish(
        session, "status", {"cmd_id": CMD_ID, "state": "failed", "detail": "boom"}
    )
    odd = await publish(session, "status", {"cmd_id": CMD_ID, "state": "teleporting"})

    assert walking is not None and walking.detail is None and walking.is_terminal is False
    assert failed is not None
    assert (failed.cmd_id, failed.state, failed.is_terminal, failed.detail) == (
        CMD_ID,
        "failed",
        True,
        "boom",
    )
    assert odd is not None and odd.state == "teleporting" and odd.is_terminal is False


async def test_a_terminal_row_carries_the_selftest_outcome(session: AsyncSession) -> None:
    """R5-be-1: parsed from the stored detail, on a terminal row of the right state only."""
    await seed_device(session)
    await seed_intent(session)

    walking = await publish(
        session,
        "status",
        {"cmd_id": CMD_ID, "state": "rolling_back", "detail": "selftest failed: boom"},
    )
    failed = await publish(
        session,
        "status",
        {
            "cmd_id": CMD_ID,
            "state": "rolled_back",
            "detail": "selftest failed: boom; returned to ota_1",
        },
    )
    replay = await publish(
        session,
        "status",
        {
            "cmd_id": CMD_ID,
            "state": "rolled_back",
            "detail": "selftest failed: boom; returned to ota_1",
        },
        retained=True,
    )

    assert walking is not None and walking.selftest is None
    assert failed is not None
    assert failed.selftest == SelfTestOutcome("failed", "boom", "ota_1")
    assert replay is None or replay.selftest is None


async def test_a_passed_selftest_rides_the_confirmed_frame(session: AsyncSession) -> None:
    await seed_device(session)
    await seed_intent(session)

    event = await publish(
        session,
        "status",
        {"cmd_id": CMD_ID, "state": "confirmed", "detail": "selftest passed", "pct": 100},
    )

    assert event is not None
    assert event.selftest == SelfTestOutcome("passed", None, None)
    assert json.loads(event.model_dump_json())["selftest"] == {
        "outcome": "passed",
        "reason": None,
        "returned_to": None,
    }


async def test_the_selftest_reason_is_the_stored_redacted_copy(session: AsyncSession) -> None:
    await seed_device(session)
    await seed_intent(session)

    event = await publish(
        session,
        "status",
        {
            "cmd_id": CMD_ID,
            "state": "rolled_back",
            "detail": "selftest failed: https://host/x?sig=secret boom; returned to ota_0",
        },
    )

    assert event is not None and event.selftest is not None
    assert event.selftest.reason == "<url> boom"
    assert "sig=secret" not in event.model_dump_json()


async def test_a_confirmed_without_selftest_words_has_a_null_selftest(
    session: AsyncSession,
) -> None:
    await seed_device(session)
    await seed_intent(session)

    event = await publish(
        session, "status", {"cmd_id": CMD_ID, "state": "confirmed", "detail": "ok"}
    )

    assert event is not None and event.is_terminal is True and event.selftest is None


async def test_a_terminal_row_without_detail_has_a_null_detail(session: AsyncSession) -> None:
    await seed_device(session)
    await seed_intent(session)

    event = await publish(session, "status", {"cmd_id": CMD_ID, "state": "confirmed"})

    assert event is not None
    assert event.is_terminal is True
    assert event.detail is None


async def test_non_deploy_events_carry_no_transaction_fields(session: AsyncSession) -> None:
    """A deduped replay, a status with no cmd_id, and a heartbeat all leave the four null."""
    await seed_device(session)
    await seed_intent(session)
    body = {"cmd_id": CMD_ID, "state": "confirmed", "detail": "ok"}
    await publish(session, "status", body)

    events = [
        await publish(session, "status", body, retained=True),
        await publish(session, "status", {"state": "idle"}),
        await publish(session, "heartbeat", {"fw_version": "1.4.2"}),
    ]

    for event in events:
        assert event is not None
        assert event.type is not EventType.DEVICE_DEPLOY
        assert (event.cmd_id, event.state, event.is_terminal, event.detail) == (
            None,
            None,
            None,
            None,
        )


def _reset_reason_lines(records: list[logging.LogRecord]) -> list[str]:
    return [r.getMessage() for r in records if "announced reset_reason=" in r.getMessage()]


async def test_a_live_announce_logs_its_reset_reason(session: AsyncSession) -> None:
    await seed_device(session)
    with capture_logs() as records:
        event = await publish(session, "announce", {**ANNOUNCE, "reset_reason": "panic"})

    assert event is not None
    assert _reset_reason_lines(records) == [f"device {DEVICE_ID} announced reset_reason=panic"]


async def test_a_retained_announce_does_not_log_a_reset_reason(session: AsyncSession) -> None:
    """A replay repeats an old boot's reason on every ingestor reconnect."""
    await seed_device(session)
    with capture_logs() as records:
        event = await publish(
            session, "announce", {**ANNOUNCE, "reset_reason": "panic"}, retained=True
        )

        assert event is not None
        assert _reset_reason_lines(records) == []
        await publish(session, "announce", {**ANNOUNCE, "reset_reason": "sw"})

    assert _reset_reason_lines(records) == [f"device {DEVICE_ID} announced reset_reason=sw"]


async def test_an_unknown_reset_reason_is_logged_as_is(session: AsyncSession) -> None:
    """Evolution rules, rule 2: a name from a later IDF is never refused."""
    await seed_device(session)
    with capture_logs() as records:
        await publish(session, "announce", {**ANNOUNCE, "reset_reason": "new_in_idf_6"})

    assert _reset_reason_lines(records) == [
        f"device {DEVICE_ID} announced reset_reason=new_in_idf_6"
    ]


@pytest.mark.parametrize(
    "value",
    [7, True, ["sw"], {"r": "sw"}, "x" * 17, "sw\nfake log line", None],
)
async def test_a_malformed_reset_reason_logs_null_and_keeps_the_announce(
    session: AsyncSession, value: object
) -> None:
    await seed_device(session)
    with capture_logs() as records:
        event = await publish(
            session, "announce", {**ANNOUNCE, "fw_version": "9.9.9", "reset_reason": value}
        )

    assert event is not None
    assert (await reload(session)).fw_version == "9.9.9"
    assert _reset_reason_lines(records) == [f"device {DEVICE_ID} announced reset_reason=null"]


async def test_an_absent_reset_reason_logs_null(session: AsyncSession) -> None:
    """Every agent <= 0.5.0."""
    await seed_device(session)
    with capture_logs() as records:
        await publish(session, "announce", ANNOUNCE)

    assert _reset_reason_lines(records) == [f"device {DEVICE_ID} announced reset_reason=null"]


def test_reset_reason_is_stored_on_every_announce_not_only_when_present() -> None:
    """R4-be-1: `store.py::_reset_values` writes `last_reset_reason` on every announce."""
    assert "reset_reason" not in ANNOUNCE_FIELDS
    assert "last_reset_reason" not in ANNOUNCE_FIELDS
    assert "last_reset_reason" in Device.__table__.columns


async def seed_command(
    session: AsyncSession,
    *,
    command_id: str = COMMAND_ID,
    device_id: str = DEVICE_ID,
    type: str = "apply",
    cmd_id: str | None = CMD_ID,
) -> None:
    """What `POST .../apply` writes before it publishes, through the shipped writer."""
    await device_commands.record_sent(
        session,
        command_id=command_id,
        device_id=device_id,
        type=type,
        cmd_id=cmd_id,
        sent_by=DeploySender(subject="admin", token_id="t", credential=None),
    )


def ack(**overrides: object) -> dict[str, object]:
    body: dict[str, object] = {
        "id": COMMAND_ID,
        "type": "apply",
        "result": "accepted",
        "cmd_id": CMD_ID,
        "detail": None,
    }
    body.update(overrides)
    return body


async def command_row(session: AsyncSession, command_id: str = COMMAND_ID) -> DeviceCommand:
    row = await session.scalar(select(DeviceCommand).where(DeviceCommand.id == command_id))
    assert row is not None
    await session.refresh(row)
    return row


async def deploy_row_count(session: AsyncSession) -> int:
    return int(await session.scalar(select(func.count()).select_from(DeployEvent)) or 0)


async def test_a_live_ack_records_the_answer_and_emits_device_command(
    session: AsyncSession,
) -> None:
    await seed_device(session)
    await seed_command(session)

    event = await publish(session, "ack", ack())

    assert event is not None
    assert event.type is EventType.DEVICE_COMMAND
    assert event.command_id == COMMAND_ID
    assert event.cmd_id is None
    row = await command_row(session)
    assert (row.result, row.answer_cmd_id, row.answer_detail, row.answered_at) == (
        "accepted",
        CMD_ID,
        None,
        NOW,
    )
    assert (await reload(session)).last_seen == NOW
    assert await deploy_row_count(session) == 0


async def test_the_first_answer_wins(session: AsyncSession) -> None:
    """A QoS-1 duplicate, or a second answer, changes nothing and is not news."""
    await seed_device(session)
    await seed_command(session)
    await publish(session, "ack", ack())

    later = NOW + dt.timedelta(seconds=5)
    event = await publish(session, "ack", ack(result="refused", detail="late"), at=later)

    assert event is not None
    assert event.type is EventType.DEVICE_SEEN
    assert event.command_id is None
    row = await command_row(session)
    assert (row.result, row.answer_detail, row.answered_at) == ("accepted", None, NOW)
    assert (await reload(session)).last_seen == later


async def test_a_board_cannot_answer_another_boards_command(session: AsyncSession) -> None:
    await seed_device(session)
    await seed_device(session, OTHER_ID)
    await seed_command(session)

    event = await publish(session, "ack", ack(), device_id=OTHER_ID)

    assert event is not None
    assert event.type is EventType.DEVICE_SEEN
    assert (await command_row(session)).result is None


async def test_an_unknown_id_creates_no_row_and_logs_only_its_length(
    session: AsyncSession,
) -> None:
    await seed_device(session)

    with capture_logs() as records:
        event = await publish(session, "ack", ack(id="bogus-id-from-a-hand-built-command"))

    assert event is not None
    assert event.type is EventType.DEVICE_SEEN
    assert await session.scalar(select(func.count()).select_from(DeviceCommand)) == 0
    lines = [r.getMessage() for r in records if "did not send" in r.getMessage()]
    assert lines == [
        f"device {DEVICE_ID} answered (accepted) a command id this server did not send it "
        "(34 chars); no row created"
    ]
    assert not any("bogus" in r.getMessage() for r in records)


async def test_a_type_mismatch_is_not_recorded(session: AsyncSession) -> None:
    await seed_device(session)
    await seed_command(session, type="reboot", cmd_id=None)

    with capture_logs() as records:
        event = await publish(session, "ack", ack(type="apply"))

    assert event is not None
    assert event.type is EventType.DEVICE_SEEN
    assert (await command_row(session)).result is None
    assert any(r.levelno == logging.WARNING and "another type" in r.getMessage() for r in records)


async def test_a_retained_ack_is_ignored_and_proves_nothing(session: AsyncSession) -> None:
    await seed_device(session)
    await seed_command(session)

    event = await publish(session, "ack", ack(), retained=True)

    assert event is None
    assert (await command_row(session)).result is None
    assert (await reload(session)).last_seen is None


async def test_an_oversized_ack_is_dropped_but_proves_liveness(session: AsyncSession) -> None:
    await seed_device(session)
    await seed_command(session)

    event = await publish(session, "ack", ack(detail="x" * 2000))

    assert event is not None
    assert event.type is EventType.DEVICE_SEEN
    assert (await command_row(session)).result is None
    assert (await reload(session)).last_seen == NOW


@pytest.mark.parametrize(
    "raw",
    [b"not json", b"[]", b'{"id": 42}', b'{"id": "c", "result": ["x"]}'],
)
async def test_a_malformed_ack_is_dropped_quietly(session: AsyncSession, raw: bytes) -> None:
    await seed_device(session)
    await seed_command(session)

    event = await publish(session, "ack", raw=raw)

    assert event is not None
    assert event.type is EventType.DEVICE_SEEN
    assert (await command_row(session)).result is None


@pytest.mark.parametrize("result", ["", "   ", "x" * 17, "bad\nline", None])
async def test_an_unusable_result_is_not_an_answer(
    session: AsyncSession, result: str | None
) -> None:
    await seed_device(session)
    await seed_command(session)

    await publish(session, "ack", ack(result=result))

    assert (await command_row(session)).result is None


async def test_an_open_result_vocabulary_is_stored_as_sent(session: AsyncSession) -> None:
    """`AckResult` is advisory: a newer agent's value is recorded, not lost."""
    await seed_device(session)
    await seed_command(session)

    await publish(session, "ack", ack(result="postponed"))

    assert (await command_row(session)).result == "postponed"


async def test_the_detail_is_stored_sanitised_and_never_logged(session: AsyncSession) -> None:
    await seed_device(session)
    await seed_command(session)
    secret = "https://downloads.test/v1/artifact/x/bin?exp=1&sig=abc"

    with capture_logs() as records:
        await publish(session, "ack", ack(result="refused", detail=f"nope\x1b[31m {secret} tail"))

    assert (await command_row(session)).answer_detail == "nope[31m <url> tail"
    assert records, "the capture is not vacuous"
    assert not any("nope" in r.getMessage() or "sig=" in r.getMessage() for r in records)


async def test_an_overlong_answer_cmd_id_is_stored_as_null(session: AsyncSession) -> None:
    await seed_device(session)
    await seed_command(session)

    await publish(session, "ack", ack(cmd_id="c" * 65))

    row = await command_row(session)
    assert (row.result, row.answer_cmd_id) == ("accepted", None)


async def test_a_publish_failed_command_takes_no_answer(session: AsyncSession) -> None:
    await seed_device(session)
    await seed_command(session)
    await device_commands.mark_publish_failed(session, command_id=COMMAND_ID)

    event = await publish(session, "ack", ack())

    assert event is not None
    assert event.type is EventType.DEVICE_SEEN
    assert (await command_row(session)).result is None


async def test_an_ack_from_an_unregistered_device_writes_nothing(session: AsyncSession) -> None:
    event = await publish(session, "ack", ack())
    assert event is None
    assert await session.scalar(select(func.count()).select_from(DeviceCommand)) == 0


async def test_no_ack_ever_writes_a_deploy_row(session: AsyncSession) -> None:
    """The ack is not a transaction state: not for an accepted apply, not for a refusal."""
    await seed_device(session)
    await seed_intent(session)
    await seed_command(session)
    await seed_command(session, command_id="r" * 32, type="rollback", cmd_id=None)
    before = await deploy_row_count(session)

    await publish(session, "ack", ack())
    await publish(
        session,
        "ack",
        ack(id="r" * 32, type="rollback", result="refused", cmd_id=None, detail="cancel first"),
    )
    await publish(session, "ack", ack(id="unknown"))

    assert await deploy_row_count(session) == before == 1


async def test_cancelled_is_recorded_as_terminal(session: AsyncSession) -> None:
    await seed_device(session)
    await seed_intent(session)

    event = await publish(session, "status", {"cmd_id": CMD_ID, "state": "cancelled"})

    assert event is not None
    assert event.type is EventType.DEVICE_DEPLOY
    assert event.is_terminal is True
    row = (await deploy_rows(session))[-1]
    assert (row.state, row.is_terminal, row.artifact_version) == ("cancelled", True, "1.5.0")
    assert counts_in_deploy_kpis(terminal_state=row.state, has_requested_row=True) is False


async def test_a_rollback_commands_own_transaction_is_recorded_and_out_of_the_kpis(
    session: AsyncSession,
) -> None:
    """D5: a rollback of a confirmed image reports under the command's own id."""
    await seed_device(session)
    await seed_command(session, type="rollback", cmd_id=None)

    await publish(session, "status", {"cmd_id": COMMAND_ID, "state": "rolling_back"})
    event = await publish(
        session,
        "status",
        {
            "cmd_id": COMMAND_ID,
            "state": "rolled_back",
            "detail": "rolled back by command; returned to ota_0",
        },
    )

    rows = await deploy_rows(session, COMMAND_ID)
    assert [(r.state, r.is_terminal) for r in rows] == [
        ("rolling_back", False),
        ("rolled_back", True),
    ]
    assert rows[-1].artifact_version is None
    assert event is not None
    assert event.selftest is None
    assert counts_in_deploy_kpis(terminal_state="rolled_back", has_requested_row=False) is False


@pytest.mark.parametrize(
    ("state", "has_requested", "counts"),
    [
        ("confirmed", True, True),
        ("rolled_back", True, True),
        ("failed", True, True),
        ("cancelled", True, False),
        ("confirmed", False, False),
        ("rolled_back", False, False),
        ("failed", False, False),
        ("cancelled", False, False),
    ],
)
def test_counts_in_deploy_kpis(state: str, has_requested: bool, counts: bool) -> None:
    assert counts_in_deploy_kpis(terminal_state=state, has_requested_row=has_requested) is counts

"""Simulator R5 verb commands (`apply`, `cancel`, `rollback`, `reboot`), safe windows, and parity (`T-10`)."""

from __future__ import annotations

import asyncio
import json
import re
from pathlib import Path
from typing import Any

import pytest

from fleetforge.broker.commands import VERB_COMMANDS as SERVER_VERB_COMMANDS
from fleetforge.db.models import TERMINAL_DEPLOY_STATES, AckResult, DeployState
from fleetforge.ingestor.protocol import UpChannel
from fleetforge.selftest_outcome import selftest_outcome
from fleetforge.simulator import device as sim_device
from fleetforge.simulator.device import (
    ACK,
    ACK_ACCEPTED,
    ACK_DEFERRED,
    ACK_DETAILS,
    ACK_REFUSED,
    ANNOUNCE,
    CONFIRM_NEVER,
    CONFIRM_WALK,
    DETAIL_APPLY_WAITS,
    DETAIL_ROLLBACK_WAITS,
    LINK_FAST,
    QOS,
    RETURNED_TO,
    ROLLED_BACK_BY_COMMAND,
    SAFE_WINDOW_FILE_PREFIX,
    SAFE_WINDOW_HOLD,
    SAFE_WINDOW_POLL_S,
    STAGE_WALK,
    STATE_APPLYING,
    STATE_AWAITING_SAFE_WINDOW,
    STATE_CANCELLED,
    STATE_CONFIRMING,
    STATE_FAILED,
    STATE_REBOOTING,
    STATE_ROLLED_BACK,
    STATE_ROLLING_BACK,
    STATE_STAGED,
    STATUS,
    VERB_COMMANDS,
    DeviceIdentity,
    LinkProfile,
    StageRunner,
    up_topic,
)
from fleetforge.simulator.errors import SimulatorError
from tests.agent_src import COMPONENT_SRC
from tests.doubles import (
    FIRMWARE,
    FakeClient,
    FakeHttpResponse,
    FakeMessage,
    applied_stage,
    channel_payloads,
    one_session,
    stage_command,
    statuses,
    transcript,
)

R5_CAPABILITIES = ("ota", "apply", "cancel", "rollback", "reboot")
SIM_ID = "a4cf12b3de90"


@pytest.fixture
def downloads(monkeypatch: pytest.MonkeyPatch) -> list[str]:
    """Record every URL fetched and serve `FIRMWARE`. One entry per real download."""
    fetched: list[str] = []

    def fake_urlopen(url: str, timeout: float | None = None) -> FakeHttpResponse:
        fetched.append(url)
        return FakeHttpResponse(FIRMWARE)

    monkeypatch.setattr("urllib.request.urlopen", fake_urlopen)
    return fetched


def for_cmd(fake: FakeClient, cmd_id: str) -> list[dict[str, Any]]:
    return [report for report in statuses(fake) if report["cmd_id"] == cmd_id]


def verb(kind: str, command_id: str, cmd_id: str | None = None) -> FakeMessage:
    """A verb exactly as `broker.commands.verb_payload` builds it (retyped, Patch A2)."""
    body: dict[str, Any] = {"id": command_id, "type": kind}
    if cmd_id is not None:
        body["cmd_id"] = cmd_id
    return FakeMessage(f"ff/v1/d/{SIM_ID}/dn/cmd", json.dumps(body).encode())


def staged_msg(cmd_id: str = "cmd-stage-1", version: str = "1.5.0") -> FakeMessage:
    return FakeMessage(
        f"ff/v1/d/{SIM_ID}/dn/cmd",
        stage_command(cmd_id=cmd_id, version=version, apply="on_command"),
    )


def acks(fake: FakeClient) -> list[dict[str, Any]]:
    return channel_payloads(fake, SIM_ID, ACK)


def r5_board(**kwargs: Any) -> StageRunner:
    capabilities = kwargs.pop("capabilities", R5_CAPABILITIES)
    return StageRunner(
        identity=DeviceIdentity(device_id=SIM_ID, fw_version="1.4.2", capabilities=capabilities),
        link=LinkProfile(LINK_FAST),
        **kwargs,
    )


async def session_with(
    stage: StageRunner, *messages: FakeMessage, awake_s: float | None = 0.2
) -> tuple[FakeClient, list[str]]:
    """One session of `stage` with `messages` queued on `dn/`; ends on a reboot or awake_s."""
    fake = FakeClient()
    for message in messages:
        fake.inbox.put_nowait(message)
    lines = await one_session(stage, fake, awake_s=awake_s)
    return fake, lines


def test_the_retyped_verb_vocabulary_is_the_servers() -> None:
    """Import purity means copies; `broker/commands.py`, `AckResult`, `UpChannel` and
    `DeployState` are the originals."""
    assert VERB_COMMANDS == SERVER_VERB_COMMANDS
    assert {ACK_ACCEPTED, ACK_DEFERRED, ACK_REFUSED} == {r.value for r in AckResult}
    assert ACK == UpChannel.ACK.value
    assert STATE_CANCELLED in {state.value for state in DeployState}
    assert DeployState(STATE_CANCELLED) in TERMINAL_DEPLOY_STATES


@pytest.mark.parametrize(
    "detail", [*ACK_DETAILS, DETAIL_APPLY_WAITS, DETAIL_ROLLBACK_WAITS, ROLLED_BACK_BY_COMMAND]
)
def test_every_ack_and_wait_detail_fits_the_firmware_buffer(detail: str) -> None:
    assert len(detail.encode()) <= 63
    assert detail.isprintable()


def test_a_command_rollback_detail_is_not_a_selftest_outcome() -> None:
    composed = f"{ROLLED_BACK_BY_COMMAND}{RETURNED_TO}ota_0"
    assert composed == "rolled back by command; returned to ota_0"
    assert selftest_outcome(STATE_ROLLED_BACK, composed) is None


async def test_apply_releases_a_staged_on_command_image(downloads: list[str]) -> None:
    _ = downloads
    stage = r5_board()
    first, _ = await session_with(
        stage, staged_msg(), verb("apply", "x1", "cmd-stage-1"), awake_s=None
    )

    assert stage.reboot.is_set()
    assert acks(first) == [
        {"id": "x1", "type": "apply", "result": "accepted", "cmd_id": "cmd-stage-1", "detail": None}
    ]
    assert list(acks(first)[0]) == ["id", "type", "result", "cmd_id", "detail"]
    assert [report["state"] for report in statuses(first)] == list(STAGE_WALK)
    ack_publishes = [p for p in first.published if p[0] == up_topic(SIM_ID, ACK)]
    assert [(qos, retain) for _, _, qos, retain in ack_publishes] == [(QOS, False)]
    topics = [topic for topic, *_ in first.published]
    applying = next(
        i
        for i, (topic, payload, *_) in enumerate(first.published)
        if topic == up_topic(SIM_ID, STATUS) and json.loads(payload)["state"] == STATE_APPLYING
    )
    assert topics.index(up_topic(SIM_ID, ACK)) < applying

    stage.reboot.clear()
    second, _ = await session_with(stage)
    assert [report["state"] for report in statuses(second)] == list(CONFIRM_WALK)
    assert channel_payloads(second, SIM_ID, ANNOUNCE)[0]["fw_version"] == "1.5.0"
    assert channel_payloads(second, SIM_ID, ANNOUNCE)[0]["reset_reason"] == "sw"


async def test_apply_with_nothing_staged_is_refused_and_changes_nothing(
    downloads: list[str],
) -> None:
    _ = downloads
    stage = r5_board()
    fake, _ = await session_with(stage, verb("apply", "x1", "nope"))

    assert acks(fake) == [
        {
            "id": "x1",
            "type": "apply",
            "result": "refused",
            "cmd_id": None,
            "detail": "nothing staged for this cmd_id",
        }
    ]
    assert statuses(fake) == []
    assert not stage.reboot.is_set()


@pytest.mark.parametrize("kind", ["apply", "cancel"])
async def test_apply_and_cancel_without_cmd_id_are_refused(kind: str) -> None:
    fake, _ = await session_with(r5_board(), verb(kind, "x1"))
    assert acks(fake)[0]["result"] == "refused"
    assert acks(fake)[0]["detail"] == "cmd_id missing"


async def test_cancel_of_a_staged_image_reports_cancelled_and_frees_the_slot(
    downloads: list[str],
) -> None:
    _ = downloads
    stage = r5_board()
    fake, _ = await session_with(
        stage,
        staged_msg(),
        verb("cancel", "x2", "cmd-stage-1"),
        staged_msg(cmd_id="cmd-stage-2", version="1.6.0"),
    )

    assert acks(fake) == [
        {
            "id": "x2",
            "type": "cancel",
            "result": "accepted",
            "cmd_id": "cmd-stage-1",
            "detail": None,
        }
    ]
    cancelled = [s for s in statuses(fake) if s["state"] == STATE_CANCELLED]
    assert cancelled == [
        {"cmd_id": "cmd-stage-1", "state": "cancelled", "pct": None, "detail": None}
    ]
    assert [s["state"] for s in for_cmd(fake, "cmd-stage-2")][-1] == STATE_STAGED
    assert stage.staged == "cmd-stage-2"
    assert not stage.reboot.is_set()


async def test_cancel_of_nothing_is_refused(downloads: list[str]) -> None:
    _ = downloads
    fake, _ = await session_with(r5_board(), verb("cancel", "x2", "cmd-stage-1"))
    assert acks(fake)[0]["detail"] == "nothing to cancel for this cmd_id"
    assert statuses(fake) == []


async def confirmed_r5_board(downloads: list[str], **kwargs: Any) -> StageRunner:
    """A board that applied and confirmed 1.5.0 in ota_1, coming from 1.4.2 in ota_0."""
    _ = downloads
    stage = await applied_stage(capabilities=R5_CAPABILITIES, **kwargs)
    second, _ = await session_with(stage)
    assert [report["state"] for report in statuses(second)] == list(CONFIRM_WALK)
    assert stage.returnable is not None
    return stage


async def test_rollback_of_a_confirmed_image_is_its_own_transaction(downloads: list[str]) -> None:
    stage = await confirmed_r5_board(downloads)

    third, _ = await session_with(stage, verb("rollback", "x4"), awake_s=None)

    assert acks(third) == [
        {"id": "x4", "type": "rollback", "result": "accepted", "cmd_id": "x4", "detail": None}
    ]
    assert statuses(third) == [
        {"cmd_id": "x4", "state": "rolling_back", "pct": None, "detail": None}
    ]
    assert stage.reboot.is_set()

    stage.reboot.clear()
    fourth, _ = await session_with(stage, verb("rollback", "x5"))
    assert statuses(fourth) == [
        {
            "cmd_id": "x4",
            "state": "rolled_back",
            "pct": None,
            "detail": "rolled back by command; returned to ota_0",
        }
    ]
    announce = channel_payloads(fourth, SIM_ID, ANNOUNCE)[0]
    assert (announce["fw_version"], announce["reset_reason"]) == ("1.4.2", "sw")
    assert acks(fourth) == [
        {
            "id": "x5",
            "type": "rollback",
            "result": "refused",
            "cmd_id": None,
            "detail": "no confirmed image to return to",
        }
    ]


async def test_rollback_on_a_fresh_board_has_nothing_to_return_to() -> None:
    stage = r5_board()
    fake, _ = await session_with(stage, verb("rollback", "x4"))
    assert acks(fake)[0]["detail"] == "no confirmed image to return to"
    assert statuses(fake) == []
    assert not stage.reboot.is_set()


async def test_rollback_and_reboot_while_staged_are_refused_with_the_stage(
    downloads: list[str],
) -> None:
    _ = downloads
    stage = r5_board()
    fake, _ = await session_with(stage, staged_msg(), verb("rollback", "x4"), verb("reboot", "x6"))

    assert [(a["type"], a["result"], a["cmd_id"], a["detail"]) for a in acks(fake)] == [
        ("rollback", "refused", "cmd-stage-1", "cancel first"),
        ("reboot", "refused", "cmd-stage-1", "an update is in progress"),
    ]
    assert not stage.reboot.is_set()


async def test_rollback_while_confirming_ends_the_stages_transaction(downloads: list[str]) -> None:
    _ = downloads
    stage = await applied_stage(capabilities=R5_CAPABILITIES, confirm=CONFIRM_NEVER)

    second, _ = await session_with(
        stage, verb("reboot", "x6"), verb("rollback", "x7"), awake_s=None
    )

    assert [(a["type"], a["result"], a["cmd_id"], a["detail"]) for a in acks(second)] == [
        ("reboot", "refused", "cmd-stage-1", "the running image is not confirmed yet"),
        ("rollback", "accepted", "cmd-stage-1", None),
    ]
    assert [s["state"] for s in statuses(second)] == [STATE_CONFIRMING, STATE_ROLLING_BACK]
    stage.reboot.clear()
    third, _ = await session_with(stage)
    assert statuses(third) == [
        {
            "cmd_id": "cmd-stage-1",
            "state": "rolled_back",
            "pct": None,
            "detail": "rolled back by command; returned to ota_0",
        }
    ]
    assert stage.returnable is None


async def test_reboot_restarts_on_the_running_image_with_no_status() -> None:
    stage = r5_board()
    first, _ = await session_with(stage, verb("reboot", "x6"), awake_s=None)

    assert acks(first) == [
        {"id": "x6", "type": "reboot", "result": "accepted", "cmd_id": None, "detail": None}
    ]
    assert statuses(first) == []
    assert stage.reboot.is_set()
    stage.reboot.clear()
    second, _ = await session_with(stage)
    announce = channel_payloads(second, SIM_ID, ANNOUNCE)[0]
    assert (announce["fw_version"], announce["reset_reason"]) == ("1.4.2", "sw")
    assert statuses(second) == []


async def test_a_held_board_defers_apply_and_still_takes_a_cancel(downloads: list[str]) -> None:
    _ = downloads
    stage = r5_board(safe_window=SAFE_WINDOW_HOLD)
    fake, _ = await session_with(
        stage,
        staged_msg(),
        verb("reboot", "x6"),
        verb("apply", "x1", "cmd-stage-1"),
        verb("apply", "x1b", "cmd-stage-1"),
        verb("cancel", "x2", "cmd-stage-1"),
        awake_s=0.4,
    )

    assert [(a["type"], a["result"], a["cmd_id"], a["detail"]) for a in acks(fake)] == [
        ("reboot", "refused", "cmd-stage-1", "an update is in progress"),
        ("apply", "deferred", "cmd-stage-1", None),
        ("apply", "accepted", "cmd-stage-1", None),
        ("cancel", "accepted", "cmd-stage-1", None),
    ]
    assert [(s["state"], s["pct"], s["detail"]) for s in statuses(fake)][-2:] == [
        (STATE_AWAITING_SAFE_WINDOW, 100, "apply waits for a safe window"),
        (STATE_CANCELLED, None, None),
    ]
    assert STATE_APPLYING not in [s["state"] for s in statuses(fake)]
    assert not stage.reboot.is_set()


async def test_a_held_board_defers_rollback_and_reboot_and_never_acts(
    downloads: list[str],
) -> None:
    stage = await confirmed_r5_board(downloads)
    stage.safe_window = SAFE_WINDOW_HOLD

    fake, _ = await session_with(stage, verb("rollback", "x4"), verb("reboot", "x6"))

    assert [(a["type"], a["result"], a["cmd_id"]) for a in acks(fake)] == [
        ("rollback", "deferred", "x4"),
        ("reboot", "deferred", None),
    ]
    assert statuses(fake) == [
        {
            "cmd_id": "x4",
            "state": "awaiting_safe_window",
            "pct": None,
            "detail": "rollback waits for a safe window",
        }
    ]
    assert not stage.reboot.is_set()


async def test_a_board_without_the_verbs_ignores_them_and_sends_no_answer(
    downloads: list[str],
) -> None:
    """D9: a pre-R5 agent ignores a verb silently."""
    _ = downloads
    stage = r5_board(capabilities=("ota",))
    fake, lines = await session_with(stage, verb("reboot", "x6"), verb("rollback", "x4"))

    assert acks(fake) == []
    assert not stage.reboot.is_set()
    assert any("not implemented by this board's agent" in line for line in lines)


async def test_an_r5_board_answers_other_types_not_implemented() -> None:
    fake, _ = await session_with(r5_board(), verb("identify", "x8"))
    assert acks(fake) == [
        {
            "id": "x8",
            "type": "identify",
            "result": "refused",
            "cmd_id": None,
            "detail": "not implemented",
        }
    ]


async def test_a_held_auto_stage_is_cancellable_and_blocks_a_second_stage(
    downloads: list[str],
) -> None:
    """R5-fw-5 (5): an `apply: auto` stage waiting for the window stays cancellable."""
    _ = downloads
    stage = r5_board(safe_window=SAFE_WINDOW_HOLD)
    fake, _ = await session_with(
        stage,
        FakeMessage(f"ff/v1/d/{SIM_ID}/dn/cmd", stage_command()),
        FakeMessage(
            f"ff/v1/d/{SIM_ID}/dn/cmd", stage_command(cmd_id="cmd-stage-2", version="1.6.0")
        ),
        verb("cancel", "x2", "cmd-stage-1"),
    )

    assert [s["state"] for s in for_cmd(fake, "cmd-stage-2")][-1] == STATE_FAILED
    assert acks(fake)[0]["result"] == "accepted"
    assert [s["state"] for s in for_cmd(fake, "cmd-stage-1")][-1] == STATE_CANCELLED


def file_window(tmp_path: Path) -> tuple[str, Path]:
    """A `file:` window on a path that does not exist yet (the moment is unsafe)."""
    flag = tmp_path / "W.safe"
    return f"{SAFE_WINDOW_FILE_PREFIX}{flag}", flag


async def release(stage: StageRunner) -> tuple[FakeClient, list[str]]:
    """Ask the window once, as `_safe_window_loop` does every poll."""
    fake = FakeClient()
    lines, step = transcript()
    await stage.release_if_safe(fake, step)  # type: ignore[arg-type]
    return fake, lines


def test_the_poll_is_the_firmwares() -> None:
    header = (COMPONENT_SRC / "ff_safe_window_internal.h").read_text()
    poll_ms = re.search(r"#define FF_SAFE_WINDOW_POLL_MS (\d+)", header)
    assert poll_ms is not None
    assert SAFE_WINDOW_POLL_S == int(poll_ms.group(1)) / 1000


def test_an_empty_file_window_is_refused_before_any_io() -> None:
    with pytest.raises(SimulatorError, match="file:PATH"):
        r5_board(safe_window=SAFE_WINDOW_FILE_PREFIX)


async def test_a_closed_file_window_defers_apply_until_the_file_appears(
    downloads: list[str], tmp_path: Path
) -> None:
    _ = downloads
    mode, flag = file_window(tmp_path)
    stage = r5_board(safe_window=mode)
    fake, _ = await session_with(stage, staged_msg(), verb("apply", "x1", "cmd-stage-1"))

    assert acks(fake) == [
        {"id": "x1", "type": "apply", "result": "deferred", "cmd_id": "cmd-stage-1", "detail": None}
    ]
    assert statuses(fake)[-1] == {
        "cmd_id": "cmd-stage-1",
        "state": "awaiting_safe_window",
        "pct": 100,
        "detail": "apply waits for a safe window",
    }
    idle, _ = await release(stage)
    assert idle.published == []
    assert stage.apply_deferred == "cmd-stage-1"
    assert not stage.reboot.is_set()

    flag.touch()
    opened, lines = await release(stage)
    assert [s["state"] for s in statuses(opened)] == [STATE_APPLYING, STATE_REBOOTING]
    assert acks(opened) == []
    assert stage.reboot.is_set()
    assert stage.pending is not None and stage.pending.cmd_id == "cmd-stage-1"
    assert stage.staged is None
    assert stage.apply_deferred is None
    assert any("the safe window opened" in line for line in lines)

    stage.reboot.clear()
    after, _ = await session_with(stage)
    assert channel_payloads(after, SIM_ID, ANNOUNCE)[0]["fw_version"] == "1.5.0"
    assert [s["state"] for s in statuses(after)] == list(CONFIRM_WALK)


async def test_an_open_file_window_accepts_apply_at_once(
    downloads: list[str], tmp_path: Path
) -> None:
    _ = downloads
    mode, flag = file_window(tmp_path)
    flag.touch()
    stage = r5_board(safe_window=mode)
    fake, _ = await session_with(
        stage, staged_msg(), verb("apply", "x1", "cmd-stage-1"), awake_s=None
    )

    assert acks(fake)[0]["result"] == "accepted"
    assert STATE_AWAITING_SAFE_WINDOW not in [s["state"] for s in statuses(fake)]
    assert [s["state"] for s in statuses(fake)] == list(STAGE_WALK)
    assert stage.reboot.is_set()


async def test_the_session_releases_the_apply_when_the_window_opens(
    downloads: list[str], tmp_path: Path
) -> None:
    """The worker polls the window inside a live session: the reboot ends it."""
    _ = downloads
    mode, flag = file_window(tmp_path)
    stage = r5_board(safe_window=mode)

    async def open_later() -> None:
        await asyncio.sleep(0.3)
        flag.touch()

    opener = asyncio.create_task(open_later())
    fake, _ = await session_with(
        stage, staged_msg(), verb("apply", "x1", "cmd-stage-1"), awake_s=None
    )
    await opener

    assert stage.reboot.is_set()
    states = [s["state"] for s in statuses(fake)]
    assert states[-4:] == [
        STATE_STAGED,
        STATE_AWAITING_SAFE_WINDOW,
        STATE_APPLYING,
        STATE_REBOOTING,
    ]


async def test_a_parked_auto_stage_is_released_when_the_window_opens(
    downloads: list[str], tmp_path: Path
) -> None:
    _ = downloads
    mode, flag = file_window(tmp_path)
    stage = r5_board(safe_window=mode)
    fake, _ = await session_with(stage, FakeMessage(f"ff/v1/d/{SIM_ID}/dn/cmd", stage_command()))

    assert statuses(fake)[-1] == {
        "cmd_id": "cmd-stage-1",
        "state": "awaiting_safe_window",
        "pct": None,
        "detail": None,
    }
    assert stage.apply_deferred == "cmd-stage-1"
    flag.touch()
    opened, _ = await release(stage)
    assert [s["state"] for s in statuses(opened)] == [STATE_APPLYING, STATE_REBOOTING]
    assert stage.identity.fw_version == "1.5.0"


async def test_a_cancelled_deferred_apply_is_never_released(
    downloads: list[str], tmp_path: Path
) -> None:
    _ = downloads
    mode, flag = file_window(tmp_path)
    stage = r5_board(safe_window=mode)
    fake, _ = await session_with(
        stage,
        staged_msg(),
        verb("apply", "x1", "cmd-stage-1"),
        verb("cancel", "x2", "cmd-stage-1"),
    )
    assert [(a["type"], a["result"]) for a in acks(fake)] == [
        ("apply", "deferred"),
        ("cancel", "accepted"),
    ]

    flag.touch()
    opened, _ = await release(stage)
    assert opened.published == []
    assert not stage.reboot.is_set()
    assert stage.pending is None
    assert stage.identity.fw_version == "1.4.2"


async def test_a_deferred_rollback_of_a_confirming_image_is_released(
    downloads: list[str], tmp_path: Path
) -> None:
    _ = downloads
    mode, flag = file_window(tmp_path)
    flag.touch()
    stage = await applied_stage(
        capabilities=R5_CAPABILITIES, confirm=CONFIRM_NEVER, safe_window=mode
    )
    flag.unlink()

    second, _ = await session_with(stage, verb("rollback", "x7"))
    assert acks(second) == [
        {
            "id": "x7",
            "type": "rollback",
            "result": "deferred",
            "cmd_id": "cmd-stage-1",
            "detail": None,
        }
    ]
    assert statuses(second)[-1]["detail"] == "rollback waits for a safe window"
    assert not stage.reboot.is_set()

    flag.touch()
    opened, _ = await release(stage)
    assert statuses(opened) == [
        {"cmd_id": "cmd-stage-1", "state": "rolling_back", "pct": None, "detail": None}
    ]
    assert stage.reboot.is_set()
    assert stage.rollback_deferred is None
    stage.reboot.clear()
    third, _ = await session_with(stage)
    assert statuses(third) == [
        {
            "cmd_id": "cmd-stage-1",
            "state": "rolled_back",
            "pct": None,
            "detail": "rolled back by command; returned to ota_0",
        }
    ]


async def test_a_deferred_rollback_of_a_confirmed_image_is_released(
    downloads: list[str], tmp_path: Path
) -> None:
    mode, flag = file_window(tmp_path)
    flag.touch()
    stage = await confirmed_r5_board(downloads, safe_window=mode)
    flag.unlink()

    fake, _ = await session_with(stage, verb("rollback", "x4"), verb("rollback", "x4b"))
    assert [(a["id"], a["result"], a["cmd_id"]) for a in acks(fake)] == [
        ("x4", "deferred", "x4"),
        ("x4b", "accepted", "x4"),
    ]
    assert not stage.reboot.is_set()

    flag.touch()
    opened, _ = await release(stage)
    assert statuses(opened) == [
        {"cmd_id": "x4", "state": "rolling_back", "pct": None, "detail": None}
    ]
    assert stage.reboot.is_set()
    stage.reboot.clear()
    after, _ = await session_with(stage)
    assert statuses(after)[0]["detail"] == "rolled back by command; returned to ota_0"
    announce = channel_payloads(after, SIM_ID, ANNOUNCE)[0]
    assert (announce["fw_version"], announce["reset_reason"]) == ("1.4.2", "sw")


async def test_a_deferred_reboot_is_released(tmp_path: Path) -> None:
    mode, flag = file_window(tmp_path)
    stage = r5_board(safe_window=mode)
    fake, _ = await session_with(stage, verb("reboot", "x6"))
    assert acks(fake)[0]["result"] == "deferred"
    assert stage.reboot_deferred
    assert stage.identity.reset_reason == "poweron"

    flag.touch()
    opened, _ = await release(stage)
    assert opened.published == []
    assert stage.reboot.is_set()
    assert not stage.reboot_deferred
    assert stage.identity.reset_reason == "sw"


async def test_hold_never_releases_even_when_asked(downloads: list[str]) -> None:
    _ = downloads
    stage = r5_board(safe_window=SAFE_WINDOW_HOLD)
    await session_with(stage, staged_msg(), verb("apply", "x1", "cmd-stage-1"))
    assert stage.apply_deferred == "cmd-stage-1"

    opened, _ = await release(stage)
    assert opened.published == []
    assert not stage.reboot.is_set()
    assert not stage.window_can_open


async def test_the_window_worker_keeps_running_while_nothing_waits(tmp_path: Path) -> None:
    """A returning worker would read as "the broker closed the stream"."""
    mode, flag = file_window(tmp_path)
    flag.touch()
    stage = r5_board(safe_window=mode)
    assert stage.window_can_open
    with pytest.raises(TimeoutError):
        await asyncio.wait_for(
            sim_device._safe_window_loop(FakeClient(), stage, lambda _m: None),  # type: ignore[arg-type]
            timeout=3 * SAFE_WINDOW_POLL_S,
        )
    assert not stage.reboot.is_set()


def test_the_simulated_board_announces_no_capability_the_firmware_lacks() -> None:
    """D-21: simulator defaults must only announce capabilities implemented in `ff_identity.c`."""
    identity_c = (
        Path(__file__).resolve().parents[1]
        / "agent"
        / "components"
        / "fleetforge"
        / "src"
        / "ff_identity.c"
    ).read_text(encoding="utf-8")
    firmware_caps = set(
        re.findall(
            r'cJSON_AddItemToArray\(capabilities,\s*cJSON_CreateString\("([^"]+)"\)\)',
            identity_c,
        )
    )
    assert {
        "ota",
        "apply",
        "cancel",
        "rollback",
        "reboot",
        "selftest",
        "safe_window",
        "sig",
    } <= firmware_caps
    assert "delta" not in firmware_caps
    assert "edge_relay" not in firmware_caps

    default_identity = sim_device.DeviceIdentity(device_id=SIM_ID, fw_version="0.7.0")
    announced = set(default_identity.capabilities)
    assert announced <= firmware_caps, (
        f"simulated board announces capabilities missing from firmware: {sorted(announced - firmware_caps)}"
    )
    assert "delta" not in announced
    assert "edge_relay" not in announced


def test_stage_runner_derives_require_signature_from_sig_capability() -> None:
    """D-26: when omitted, `StageRunner.require_signature` matches `'sig' in capabilities`."""
    signed_id = sim_device.DeviceIdentity(
        device_id=SIM_ID, fw_version="0.7.0", capabilities=("ota", "sig")
    )
    unsigned_id = sim_device.DeviceIdentity(
        device_id=SIM_ID, fw_version="0.7.0", capabilities=("ota",)
    )
    assert StageRunner(identity=signed_id, link=LinkProfile(LINK_FAST)).require_signature is True
    assert StageRunner(identity=unsigned_id, link=LinkProfile(LINK_FAST)).require_signature is False
    assert (
        StageRunner(
            identity=signed_id, link=LinkProfile(LINK_FAST), require_signature=False
        ).require_signature
        is False
    )

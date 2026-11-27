"""Simulator OTA stage, confirm, rollback, `boot_ok`, broken-marker, and self-test suites (`T-10`)."""

from __future__ import annotations

import asyncio
from typing import Any

import pytest

from fleetforge.db.models import DeployState
from fleetforge.selftest_outcome import SelfTestOutcome, parse_selftest_detail
from fleetforge.simulator import __main__ as cli
from fleetforge.simulator.device import (
    ANNOUNCE,
    CAPABILITY_SELFTEST,
    CONFIRM_AUTO,
    CONFIRM_NEVER,
    CONFIRM_WALK,
    DEFAULT_CONFIRM_TIMEOUT_S,
    HEARTBEAT,
    LINK_FAST,
    PRESENCE_OFFLINE,
    RETURNED_TO,
    ROLLBACK_WALK,
    SAFE_WINDOW_AUTO,
    SAFE_WINDOW_HOLD,
    SELFTEST_FAIL_REASON,
    SELFTEST_FAILED,
    SELFTEST_PASSED,
    SELFTEST_TIMED_OUT,
    STAGE_WALK,
    STATE_APPLYING,
    STATE_AWAITING_SAFE_WINDOW,
    STATE_CONFIRMED,
    STATE_CONFIRMING,
    STATE_DOWNLOADING,
    STATE_FAILED,
    STATE_REBOOTING,
    STATE_ROLLED_BACK,
    STATE_ROLLING_BACK,
    STATE_STAGED,
    STATE_STAGING,
    STATE_VERIFYING,
    STATUS,
    DeviceIdentity,
    LinkProfile,
    PendingConfirm,
    StageRunner,
    redact_url,
    redacted_command,
    run_always_on,
    run_sleepy,
    up_topic,
)
from fleetforge.simulator.errors import SimulatorError
from tests.doubles import (
    FIRMWARE,
    FIRMWARE_SHA256,
    SIGNED_URL,
    FakeClient,
    FakeHttpResponse,
    FakeMessage,
    applied_stage,
    channel_payloads,
    credential_for,
    drive_session,
    drive_until,
    one_session,
    run_stage,
    session_factory,
    stage_command,
    statuses,
    transcript,
    wait_until,
)


@pytest.fixture
def downloads(monkeypatch: pytest.MonkeyPatch) -> list[str]:
    """Record every URL fetched and serve `FIRMWARE`. One entry per real download."""
    fetched: list[str] = []

    def fake_urlopen(url: str, timeout: float | None = None) -> FakeHttpResponse:
        fetched.append(url)
        return FakeHttpResponse(FIRMWARE)

    monkeypatch.setattr("urllib.request.urlopen", fake_urlopen)
    return fetched


def test_the_retyped_states_are_the_servers_states() -> None:
    """Import purity means these are copies; `DeployState` is the original."""
    values = {state.value for state in DeployState}
    for state in (*STAGE_WALK, STATE_AWAITING_SAFE_WINDOW, STATE_FAILED):
        assert state in values, f"{state} is not a DeployState"
    assert STATE_STAGED in values and STATE_VERIFYING in values
    outcome = (STATE_CONFIRMING, STATE_CONFIRMED, STATE_ROLLING_BACK, STATE_ROLLED_BACK)
    for state in outcome:
        assert state in values, f"{state} is not a DeployState"
    assert CONFIRM_WALK == (STATE_CONFIRMING, STATE_CONFIRMED)
    assert ROLLBACK_WALK == (STATE_CONFIRMING, STATE_ROLLING_BACK, STATE_ROLLED_BACK)


def test_the_stage_walk_is_the_spec_order_and_does_not_assume_a_safe_window() -> None:
    """`awaiting_safe_window` is conditional, so it is not part of the straight-line walk."""
    assert STAGE_WALK == (
        STATE_STAGING,
        STATE_DOWNLOADING,
        STATE_VERIFYING,
        STATE_STAGED,
        STATE_APPLYING,
        STATE_REBOOTING,
    )
    assert STATE_AWAITING_SAFE_WINDOW not in STAGE_WALK


async def test_a_stage_walks_the_states_and_every_status_is_retained(
    downloads: list[str],
) -> None:
    """Retained because `spec/device-protocol.md` says so: the transaction must survive."""
    _ = downloads
    _, fake, _ = await run_stage(stage_command())

    reports = statuses(fake)
    assert [report["state"] for report in reports] == list(STAGE_WALK)
    assert all(report["cmd_id"] == "cmd-stage-1" for report in reports)
    status_publishes = [
        (qos, retain)
        for topic, _, qos, retain in fake.published
        if topic == up_topic("a4cf12b3de90", STATUS)
    ]
    assert status_publishes == [(1, True)] * len(STAGE_WALK)


async def test_the_bytes_are_downloaded_once_and_verified(downloads: list[str]) -> None:
    _, _, lines = await run_stage(stage_command())

    assert downloads == [SIGNED_URL]
    assert any(f"{len(FIRMWARE)} bytes" in line for line in lines)
    assert any(FIRMWARE_SHA256 in line and "matches" in line for line in lines)


async def test_a_duplicated_command_produces_exactly_one_download(
    downloads: list[str],
) -> None:
    """The dedup rule with teeth: two publishes of one intent, one flash write."""
    _, fake, lines = await run_stage(stage_command(), repeats=2)

    assert len(downloads) == 1
    assert [report["state"] for report in statuses(fake)] == list(STAGE_WALK)
    assert any("DUPLICATE" in line for line in lines)


async def test_a_sha256_mismatch_fails_and_never_applies(downloads: list[str]) -> None:
    """Unverified bytes are never applied — the point of putting the digest in the command."""
    _ = downloads
    _, fake, lines = await run_stage(stage_command(sha256="b" * 64))

    states = [report["state"] for report in statuses(fake)]
    assert states == [STATE_STAGING, STATE_DOWNLOADING, STATE_VERIFYING, STATE_FAILED]
    assert STATE_APPLYING not in states and STATE_REBOOTING not in states
    assert statuses(fake)[-1]["detail"] == "sha256 mismatch"
    assert any("MISMATCH" in line for line in lines)


@pytest.mark.parametrize(
    "sha256",
    [
        "abc",
        FIRMWARE_SHA256.upper(),
        FIRMWARE_SHA256 + "0",
        "g" * 64,
    ],
)
async def test_a_malformed_digest_is_refused_before_any_download(
    downloads: list[str], sha256: str
) -> None:
    """R2-fw-1: the firmware refuses a misspelt digest at the command contract boundary, before I/O."""
    _, fake, _ = await run_stage(stage_command(sha256=sha256))

    reports = statuses(fake)
    assert [report["state"] for report in reports] == [STATE_FAILED]
    assert reports[-1]["detail"] == "artifact sha256 malformed"
    assert downloads == []


async def test_an_artifact_larger_than_the_slot_is_refused_before_download(
    downloads: list[str],
) -> None:
    """R2-fw-1: one byte over the 1966080-byte slot; nothing is fetched."""
    _, fake, lines = await run_stage(stage_command(size=1966081))

    reports = statuses(fake)
    assert [report["state"] for report in reports] == [STATE_STAGING, STATE_FAILED]
    assert reports[-1]["detail"] == "artifact larger than the ota slot"
    assert downloads == []
    assert any("1966081" in line for line in lines)


async def test_a_download_failure_is_reported_not_retried_silently(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    def explode(url: str, timeout: float | None = None) -> FakeHttpResponse:
        raise OSError("connection reset")

    monkeypatch.setattr("urllib.request.urlopen", explode)
    _, fake, _ = await run_stage(stage_command())

    states = [report["state"] for report in statuses(fake)]
    assert states == [STATE_STAGING, STATE_DOWNLOADING, STATE_FAILED]
    assert "download failed" in statuses(fake)[-1]["detail"]


async def test_safe_window_hold_parks_and_never_expires(downloads: list[str]) -> None:
    """Nothing server-side may end this: a vehicle in motion reports honestly, for days."""
    _ = downloads
    _, fake, lines = await run_stage(stage_command(), safe_window=SAFE_WINDOW_HOLD, awake_s=0.4)

    states = [report["state"] for report in statuses(fake)]
    assert states == [
        STATE_STAGING,
        STATE_DOWNLOADING,
        STATE_VERIFYING,
        STATE_STAGED,
        STATE_AWAITING_SAFE_WINDOW,
    ]
    assert STATE_APPLYING not in states
    assert STATE_FAILED not in states, "parking is not a failure and must not look like one"
    assert any("indefinitely" in line for line in lines)


async def test_apply_on_command_stops_at_staged(downloads: list[str]) -> None:
    """The `apply` command is R2; until then the board sits staged rather than guessing."""
    _ = downloads
    _, fake, _ = await run_stage(stage_command(apply="on_command"))

    states = [report["state"] for report in statuses(fake)]
    assert states[-1] == STATE_STAGED
    assert STATE_APPLYING not in states


async def test_the_board_comes_back_running_the_new_version(downloads: list[str]) -> None:
    _ = downloads
    stage, _, _ = await run_stage(stage_command(version="1.5.0"))

    assert stage.identity.fw_version == "1.5.0"
    assert stage.downloads == 1


async def test_the_transcript_prints_the_command_with_the_url_redacted(
    downloads: list[str],
) -> None:
    """The only place a human can see a `dn/` payload — and it is a bearer credential."""
    _ = downloads
    _, _, lines = await run_stage(stage_command())

    joined = "\n".join(lines)
    assert "X-Goog-Signature" not in joined
    assert SIGNED_URL not in joined
    assert "https://storage.invalid/…" in joined
    assert '"type": "stage"' in joined
    assert FIRMWARE_SHA256 in joined


async def test_an_apply_ends_the_session_and_the_next_one_reports_the_new_version(
    downloads: list[str], monkeypatch: pytest.MonkeyPatch
) -> None:
    """S0-test-4: `fw_version` converges across session reconnect without intervention."""
    _ = downloads
    monkeypatch.setattr("fleetforge.simulator.device.REBOOT_DELAY_S", 0.0)
    identity = DeviceIdentity(device_id="a4cf12b3de90", fw_version="1.4.2", capabilities=("ota",))
    clients, factory = session_factory(identity.device_id, stage_command(version="1.6.0"))
    stop = asyncio.Event()
    lines, step = transcript()

    await drive_until(
        run_always_on(
            identity,
            credential_for(identity.device_id),
            client_factory=factory,
            link=LinkProfile(LINK_FAST),
            heartbeat_interval_s=0.01,
            stop=stop,
            step=step,
        ),
        lambda: (
            len(clients) == 2 and channel_payloads(clients[1], identity.device_id, HEARTBEAT) != []
        ),
        stop,
    )

    assert len(clients) == 2, "the apply did not end the session, so no new version is announced"
    before, after = clients
    device_id = identity.device_id
    assert channel_payloads(before, device_id, ANNOUNCE)[0]["fw_version"] == "1.4.2"
    assert channel_payloads(after, device_id, ANNOUNCE)[0]["fw_version"] == "1.6.0"
    assert channel_payloads(after, device_id, HEARTBEAT)[0]["fw_version"] == "1.6.0"
    assert channel_payloads(before, device_id, ANNOUNCE)[0]["reset_reason"] == "poweron"
    assert channel_payloads(after, device_id, ANNOUNCE)[0]["reset_reason"] == "sw"
    assert [report["state"] for report in statuses(before)] == list(STAGE_WALK)
    assert PRESENCE_OFFLINE not in [payload for _, payload, _, _ in before.published]
    assert any(line.startswith("reboot") for line in lines)
    await wait_until(lambda: len(statuses(after)) == len(CONFIRM_WALK))
    assert [report["state"] for report in statuses(after)] == list(CONFIRM_WALK)
    assert {report["cmd_id"] for report in statuses(after)} == {"cmd-stage-1"}


async def test_an_apply_with_no_version_still_reboots(
    downloads: list[str], monkeypatch: pytest.MonkeyPatch
) -> None:
    """The reboot is the consequence of the apply, not of the payload naming a version."""
    _ = downloads
    monkeypatch.setattr("fleetforge.simulator.device.REBOOT_DELAY_S", 0.0)
    identity = DeviceIdentity(device_id="a4cf12b3de90", fw_version="1.4.2")
    clients, factory = session_factory(identity.device_id, stage_command(version=""))
    stop = asyncio.Event()

    await drive_until(
        run_always_on(
            identity,
            credential_for(identity.device_id),
            client_factory=factory,
            link=LinkProfile(LINK_FAST),
            heartbeat_interval_s=0.01,
            stop=stop,
        ),
        lambda: len(clients) == 2,
        stop,
    )

    assert channel_payloads(clients[1], identity.device_id, ANNOUNCE)[0]["fw_version"] == "1.4.2"
    assert channel_payloads(clients[1], identity.device_id, ANNOUNCE)[0]["reset_reason"] == "sw"


async def test_a_sleepy_board_reports_the_new_version_on_its_next_wake(
    downloads: list[str],
) -> None:
    """Same convergence, one duty cycle later."""
    _ = downloads
    identity = DeviceIdentity(
        device_id="a4cf12b3de90",
        fw_version="1.4.2",
        power_class="sleepy",
        expected_wake_interval_s=60,
        capabilities=("ota",),
    )
    clients, factory = session_factory(identity.device_id, stage_command(version="1.6.0"))
    stop = asyncio.Event()

    await drive_until(
        run_sleepy(
            identity,
            credential_for(identity.device_id),
            client_factory=factory,
            link=LinkProfile(LINK_FAST),
            heartbeat_interval_s=0.01,
            wake_interval_s=0.02,
            awake_s=0.3,
            stop=stop,
        ),
        lambda: (
            len(clients) == 2 and channel_payloads(clients[1], identity.device_id, HEARTBEAT) != []
        ),
        stop,
    )

    assert channel_payloads(clients[1], identity.device_id, ANNOUNCE)[0]["fw_version"] == "1.6.0"
    assert channel_payloads(clients[1], identity.device_id, HEARTBEAT)[0]["fw_version"] == "1.6.0"


async def test_a_session_that_applies_nothing_is_not_cut_short(downloads: list[str]) -> None:
    """The tripwire for the obvious wrong fix: reconnecting on every command."""
    _ = downloads
    _, fake, _ = await run_stage(stage_command(apply="on_command"), awake_s=0.15)

    assert fake.enters == 1
    assert any(payload == PRESENCE_OFFLINE for _, payload, _, _ in fake.published), (
        "a session that ended without applying anything still owes its goodbye"
    )


async def test_a_confirming_image_reports_confirmed_once_and_then_nothing(
    downloads: list[str],
) -> None:
    """`confirming` -> `confirmed`, both retained, both for the applied cmd_id, after announce."""
    _ = downloads
    stage = await applied_stage()
    assert stage.pending is not None and stage.pending.cmd_id == "cmd-stage-1"
    assert stage.pending.previous_identity.fw_version == "1.4.2"

    second = FakeClient()
    await one_session(stage, second)

    assert channel_payloads(second, "a4cf12b3de90", ANNOUNCE)[0]["fw_version"] == "1.5.0"
    reports = statuses(second)
    assert [report["state"] for report in reports] == list(CONFIRM_WALK)
    assert {report["cmd_id"] for report in reports} == {"cmd-stage-1"}
    assert reports[-1]["pct"] == 100
    status_topic = up_topic("a4cf12b3de90", STATUS)
    assert [
        (qos, retain) for topic, _, qos, retain in second.published if topic == status_topic
    ] == [(1, True)] * len(CONFIRM_WALK)
    assert second.calls.index(f"publish {up_topic('a4cf12b3de90', ANNOUNCE)}") < second.calls.index(
        f"publish {status_topic}"
    )
    assert stage.pending is None

    third = FakeClient()
    await one_session(stage, third)
    assert statuses(third) == []


async def test_an_image_that_never_confirms_rolls_back_and_the_old_one_reports_it(
    downloads: list[str], monkeypatch: pytest.MonkeyPatch
) -> None:
    """The whole rollback, through `run_always_on`: timer fires, old version reports `rolled_back`."""
    _ = downloads
    monkeypatch.setattr("fleetforge.simulator.device.REBOOT_DELAY_S", 0.0)
    identity = DeviceIdentity(device_id="a4cf12b3de90", fw_version="1.4.2", capabilities=("ota",))
    clients, factory = session_factory(identity.device_id, stage_command(version="1.5.0"))
    stop = asyncio.Event()
    lines, step = transcript()

    await drive_until(
        run_always_on(
            identity,
            credential_for(identity.device_id),
            client_factory=factory,
            link=LinkProfile(LINK_FAST),
            heartbeat_interval_s=0.01,
            stop=stop,
            confirm=CONFIRM_NEVER,
            confirm_timeout_s=0.2,
            step=step,
        ),
        lambda: len(clients) == 3 and statuses(clients[2]) != [],
        stop,
    )

    device_id = identity.device_id
    applied, on_new, back = clients[:3]
    assert [report["state"] for report in statuses(applied)] == list(STAGE_WALK)
    assert channel_payloads(on_new, device_id, ANNOUNCE)[0]["fw_version"] == "1.5.0"
    assert [report["state"] for report in statuses(on_new)] == [
        STATE_CONFIRMING,
        STATE_ROLLING_BACK,
    ]
    assert channel_payloads(back, device_id, ANNOUNCE)[0]["fw_version"] == "1.4.2"
    assert channel_payloads(back, device_id, HEARTBEAT)[0]["fw_version"] == "1.4.2"
    assert [
        channel_payloads(fake, device_id, ANNOUNCE)[0]["reset_reason"]
        for fake in (applied, on_new, back)
    ] == ["poweron", "sw", "sw"]
    rolled = statuses(back)
    assert [report["state"] for report in rolled] == [STATE_ROLLED_BACK]
    assert rolled[0]["cmd_id"] == "cmd-stage-1"
    assert "1.4.2" in rolled[0]["detail"]
    everything = [r["state"] for fake in clients for r in statuses(fake)]
    assert STATE_CONFIRMED not in everything
    assert everything.count(STATE_ROLLED_BACK) == 1
    assert PRESENCE_OFFLINE not in [payload for _, payload, _, _ in on_new.published]
    assert sum(line.startswith("reboot") for line in lines) >= 2


async def test_stop_during_the_confirm_timer_cancels_it_cleanly(downloads: list[str]) -> None:
    """Ctrl-C while an image waits to be confirmed: no rollback, and the goodbye still goes."""
    _ = downloads
    stage = await applied_stage(confirm=CONFIRM_NEVER, confirm_timeout_s=30.0)
    stop = asyncio.Event()
    second = FakeClient()
    asyncio.get_running_loop().call_later(0.1, stop.set)

    await one_session(stage, second, awake_s=None, stop=stop)

    assert [report["state"] for report in statuses(second)] == [STATE_CONFIRMING]
    assert any(payload == PRESENCE_OFFLINE for _, payload, _, _ in second.published)
    assert stage.rolled_back is None
    assert stage.pending is not None, "nothing confirmed and nothing rolled back"
    assert stage.identity.fw_version == "1.5.0"
    assert not stage.reboot.is_set()


async def test_a_reconnect_resumes_the_confirm_timer_rather_than_restarting_it(
    downloads: list[str],
) -> None:
    """The firmware arms its timer once per boot; a broker blip does not buy more time."""
    _ = downloads
    fake_now = [100.0]
    stage = await applied_stage(
        confirm=CONFIRM_NEVER, confirm_timeout_s=0.30, clock=lambda: fake_now[0]
    )
    await one_session(stage, FakeClient(), awake_s=0.05)
    assert stage.pending is not None
    assert not stage.reboot.is_set()

    fake_now[0] += 0.27
    second = FakeClient()
    await one_session(stage, second, awake_s=0.12)

    assert [report["state"] for report in statuses(second)] == [
        STATE_CONFIRMING,
        STATE_ROLLING_BACK,
    ], "the timer restarted from zero on reconnect instead of resuming remaining budget"
    assert stage.reboot.is_set() and stage.rolled_back == "cmd-stage-1"


def boot_oks(fake: FakeClient) -> list[object]:
    return [hb["boot_ok"] for hb in channel_payloads(fake, "a4cf12b3de90", HEARTBEAT)]


async def test_a_heartbeat_inside_the_confirm_window_says_boot_ok_false(
    downloads: list[str],
) -> None:
    _ = downloads
    stage = await applied_stage(confirm=CONFIRM_NEVER, confirm_timeout_s=0.3)
    assert stage.boot_ok is False, "rebooted into an unconfirmed image"

    on_new = FakeClient()
    await one_session(stage, on_new, awake_s=None)
    assert channel_payloads(on_new, "a4cf12b3de90", HEARTBEAT)[0]["fw_version"] == "1.5.0"
    assert boot_oks(on_new) == [False]
    assert [report["state"] for report in statuses(on_new)] == [
        STATE_CONFIRMING,
        STATE_ROLLING_BACK,
    ]

    stage.reboot.clear()
    back = FakeClient()
    await one_session(stage, back)
    assert channel_payloads(back, "a4cf12b3de90", HEARTBEAT)[0]["fw_version"] == "1.4.2"
    assert boot_oks(back) == [True], "the image it returned to is the confirmed one"


async def test_a_heartbeat_after_the_confirm_says_boot_ok_true(downloads: list[str]) -> None:
    _ = downloads
    stage = await applied_stage(confirm=CONFIRM_AUTO)
    assert stage.boot_ok is False, "not confirmed until the announce is acknowledged"

    on_new = FakeClient()
    await one_session(stage, on_new)
    assert [report["state"] for report in statuses(on_new)] == list(CONFIRM_WALK)
    assert channel_payloads(on_new, "a4cf12b3de90", HEARTBEAT)[0]["fw_version"] == "1.5.0"
    assert boot_oks(on_new) == [True]
    assert stage.boot_ok is True


def test_boot_ok_stays_true_on_the_running_image_after_an_apply() -> None:
    """An apply sets `pending` and `reboot` together: the running image stays confirmed until reboot."""
    stage = StageRunner(
        identity=DeviceIdentity(device_id="a4cf12b3de90", fw_version="1.4.2"),
        link=LinkProfile(LINK_FAST),
    )
    assert stage.boot_ok is True
    stage.pending = PendingConfirm(cmd_id="cmd-stage-1", previous_identity=stage.identity)
    stage.reboot.set()
    assert stage.boot_ok is True
    stage.reboot.clear()
    assert stage.boot_ok is False


def for_cmd(fake: FakeClient, cmd_id: str) -> list[dict[str, Any]]:
    return [report for report in statuses(fake) if report["cmd_id"] == cmd_id]


async def test_a_second_stage_over_an_on_command_stage_is_refused_before_download(
    downloads: list[str],
) -> None:
    stage = StageRunner(
        identity=DeviceIdentity(device_id="a4cf12b3de90", fw_version="1.4.2"),
        link=LinkProfile(LINK_FAST),
    )
    fake = FakeClient()
    for body in (
        stage_command(cmd_id="cmd-1", apply="on_command"),
        stage_command(cmd_id="cmd-2", version="1.6.0", apply="on_command"),
    ):
        fake.inbox.put_nowait(FakeMessage("ff/v1/d/a4cf12b3de90/dn/cmd", body))

    lines = await one_session(stage, fake, awake_s=0.3)

    assert [report["state"] for report in for_cmd(fake, "cmd-1")][-1] == STATE_STAGED
    refused = for_cmd(fake, "cmd-2")
    assert [report["state"] for report in refused] == [STATE_STAGING, STATE_FAILED]
    assert refused[-1]["detail"] == "an update is already staged and waits for a reboot"
    assert len(downloads) == 1
    assert stage.staged == "cmd-1", "the refusal must not disturb the staged transaction"
    assert any("waits for a reboot" in line for line in lines)


async def unconfirmed_then_refused() -> tuple[StageRunner, FakeClient]:
    """A `--confirm never` image whose confirm window receives a new stage (`cmd-2`)."""
    stage = await applied_stage(confirm=CONFIRM_NEVER, confirm_timeout_s=0.3)
    second = FakeClient()
    second.inbox.put_nowait(
        FakeMessage("ff/v1/d/a4cf12b3de90/dn/cmd", stage_command(cmd_id="cmd-2", version="1.6.0"))
    )
    await one_session(stage, second, awake_s=None)
    return stage, second


async def test_a_stage_while_the_image_is_still_confirming_is_refused(
    downloads: list[str],
) -> None:
    stage, second = await unconfirmed_then_refused()

    refused = for_cmd(second, "cmd-2")
    assert [report["state"] for report in refused] == [STATE_STAGING, STATE_FAILED]
    assert refused[-1]["detail"] == "the running image is not confirmed yet"
    assert len(downloads) == 1, "only the applied image was ever fetched"
    assert [report["state"] for report in for_cmd(second, "cmd-stage-1")] == [
        STATE_CONFIRMING,
        STATE_ROLLING_BACK,
    ]
    assert stage.reboot.is_set() and stage.rolled_back == "cmd-stage-1"


async def test_after_a_rollback_a_new_stage_is_accepted(downloads: list[str]) -> None:
    stage, _ = await unconfirmed_then_refused()
    stage.reboot.clear()
    back = FakeClient()
    await one_session(stage, back)
    assert [report["state"] for report in statuses(back)] == [STATE_ROLLED_BACK]
    assert stage.pending is None and stage.staged is None
    stage.reboot.clear()

    third = FakeClient()
    third.inbox.put_nowait(
        FakeMessage("ff/v1/d/a4cf12b3de90/dn/cmd", stage_command(cmd_id="cmd-3", version="1.6.0"))
    )
    await one_session(stage, third, awake_s=None)

    assert [report["state"] for report in for_cmd(third, "cmd-3")] == list(STAGE_WALK)
    assert len(downloads) == 2


async def test_a_session_with_no_transaction_publishes_no_status() -> None:
    fake = await drive_session(DeviceIdentity(device_id="a4cf12b3de90"))
    assert statuses(fake) == []


def test_an_unknown_confirm_mode_or_timeout_is_refused_before_any_io() -> None:
    identity = DeviceIdentity(device_id="a4cf12b3de90")
    with pytest.raises(SimulatorError):
        StageRunner(identity=identity, link=LinkProfile(LINK_FAST), confirm="sometimes")
    with pytest.raises(SimulatorError):
        StageRunner(identity=identity, link=LinkProfile(LINK_FAST), confirm_timeout_s=0)


def test_the_cli_offers_both_confirm_modes() -> None:
    args = cli.build_parser().parse_args(["run", "--confirm", "never", "--confirm-timeout", "5"])
    assert (args.confirm, args.confirm_timeout) == (CONFIRM_NEVER, 5.0)
    defaults = cli.build_parser().parse_args(["fleet"])
    assert (defaults.confirm, defaults.confirm_timeout) == (CONFIRM_AUTO, DEFAULT_CONFIRM_TIMEOUT_S)
    assert DEFAULT_CONFIRM_TIMEOUT_S == 300.0


async def test_an_image_carrying_the_broken_marker_never_confirms_though_confirm_is_auto(
    downloads: list[str],
) -> None:
    _ = downloads
    stage = await applied_stage(
        version="1.5.0-rbtest", broken_marker="-rbtest", confirm_timeout_s=0.2
    )
    second = FakeClient()
    await one_session(stage, second, awake_s=None)

    assert [report["state"] for report in statuses(second)] == [
        STATE_CONFIRMING,
        STATE_ROLLING_BACK,
    ]
    assert stage.identity.fw_version == "1.4.2"
    assert stage.reboot.is_set() and stage.rolled_back == "cmd-stage-1"
    stage.reboot.clear()

    back = FakeClient()
    await one_session(stage, back)
    assert channel_payloads(back, "a4cf12b3de90", ANNOUNCE)[0]["fw_version"] == "1.4.2"
    rolled = statuses(back)
    assert [report["state"] for report in rolled] == [STATE_ROLLED_BACK]
    assert "returned to 1.4.2" in rolled[0]["detail"]


async def test_an_image_without_the_broken_marker_still_confirms(downloads: list[str]) -> None:
    _ = downloads
    stage = await applied_stage(version="1.5.0", broken_marker="-rbtest", confirm_timeout_s=0.2)
    second = FakeClient()
    await one_session(stage, second)

    assert [report["state"] for report in statuses(second)] == list(CONFIRM_WALK)
    assert stage.pending is None and not stage.reboot.is_set()
    assert stage.rolled_back is None


async def test_one_board_confirms_a_good_build_then_rolls_back_a_broken_one(
    downloads: list[str],
) -> None:
    """Good then broken on ONE runner: rollback lands on the good build's version."""
    _ = downloads
    stage = await applied_stage(broken_marker="-rbtest", confirm_timeout_s=0.2)
    await one_session(stage, FakeClient())
    assert stage.identity.fw_version == "1.5.0" and stage.pending is None

    broken = FakeClient()
    broken.inbox.put_nowait(
        FakeMessage(
            "ff/v1/d/a4cf12b3de90/dn/cmd",
            stage_command(cmd_id="cmd-stage-2", version="1.6.0-rbtest"),
        )
    )
    await one_session(stage, broken, awake_s=None)
    assert stage.reboot.is_set(), "the second apply did not reboot"
    stage.reboot.clear()

    on_broken = FakeClient()
    await one_session(stage, on_broken, awake_s=None)
    assert [r["state"] for r in statuses(on_broken)] == [STATE_CONFIRMING, STATE_ROLLING_BACK]
    assert stage.identity.fw_version == "1.5.0"
    stage.reboot.clear()

    back = FakeClient()
    await one_session(stage, back)
    assert channel_payloads(back, "a4cf12b3de90", ANNOUNCE)[0]["fw_version"] == "1.5.0"
    rolled = statuses(back)
    assert [(r["cmd_id"], r["state"]) for r in rolled] == [("cmd-stage-2", STATE_ROLLED_BACK)]
    assert "returned to 1.5.0" in rolled[0]["detail"]


def test_an_empty_broken_marker_is_refused_before_any_io() -> None:
    identity = DeviceIdentity(device_id="a4cf12b3de90")
    with pytest.raises(SimulatorError):
        StageRunner(identity=identity, link=LinkProfile(LINK_FAST), broken_marker="")


def test_the_cli_takes_a_broken_marker_spelled_with_an_equals_sign() -> None:
    args = cli.build_parser().parse_args(["fleet", "--broken-marker=-rbtest"])
    assert args.broken_marker == "-rbtest"
    assert cli.build_parser().parse_args(["fleet"]).broken_marker is None
    with pytest.raises(SystemExit):
        cli.build_parser().parse_args(["fleet", "--broken-marker", "-rbtest"])


def test_redact_url_keeps_the_origin_and_drops_everything_else() -> None:
    assert redact_url(SIGNED_URL) == "https://storage.invalid/…"
    assert redact_url("not a url") == "<unparseable url>"


def test_redacted_command_leaves_a_command_without_an_artifact_alone() -> None:
    body: dict[str, object] = {"id": "x", "type": "reboot"}
    assert redacted_command(body) == body


def test_an_unknown_safe_window_mode_is_refused_before_any_io() -> None:
    with pytest.raises(SimulatorError):
        StageRunner(
            identity=DeviceIdentity(device_id="a4cf12b3de90"),
            link=LinkProfile(LINK_FAST),
            safe_window="whenever",
        )


def test_the_cli_offers_both_safe_window_modes_and_a_file_window() -> None:
    args = cli.build_parser().parse_args(["run", "--safe-window", "hold"])
    assert args.safe_window == SAFE_WINDOW_HOLD
    assert cli.build_parser().parse_args(["run"]).safe_window == SAFE_WINDOW_AUTO
    assert (
        cli.build_parser().parse_args(["fleet", "--safe-window", "file:/x"]).safe_window
        == "file:/x"
    )


@pytest.mark.parametrize("mode", ["whenever", "file:", "File:/x"])
def test_the_cli_refuses_a_bad_safe_window_before_any_token_is_spent(mode: str) -> None:
    with pytest.raises(SystemExit):
        cli.build_parser().parse_args(["fleet", "--safe-window", mode])


SELFTEST_CAPS = (CAPABILITY_SELFTEST,)


def parsed_details(*fakes: FakeClient) -> list[SelfTestOutcome | None]:
    return [parse_selftest_detail(r.get("detail")) for fake in fakes for r in statuses(fake)]


async def test_a_registered_selftest_that_passes_reports_it_on_the_confirmed(
    downloads: list[str],
) -> None:
    _ = downloads
    stage = await applied_stage(capabilities=SELFTEST_CAPS, selftest_fail_marker="-stfail")
    on_new = FakeClient()
    await one_session(stage, on_new)

    reports = statuses(on_new)
    assert [r["state"] for r in reports] == list(CONFIRM_WALK)
    assert reports[-1]["detail"] == "selftest passed" == SELFTEST_PASSED
    assert reports[-1]["pct"] == 100
    assert parse_selftest_detail(reports[-1]["detail"]) == SelfTestOutcome("passed", None, None)


async def test_a_board_without_a_selftest_confirms_with_no_detail(downloads: list[str]) -> None:
    _ = downloads
    stage = await applied_stage()
    on_new = FakeClient()
    await one_session(stage, on_new)

    assert statuses(on_new)[-1].get("detail") is None


async def test_a_failing_selftest_rolls_back_at_once_and_names_the_slot(
    downloads: list[str],
) -> None:
    _ = downloads
    stage = await applied_stage(
        version="1.5.0-stfail", capabilities=SELFTEST_CAPS, selftest_fail_marker="-stfail"
    )
    on_new = FakeClient()
    await one_session(stage, on_new, awake_s=None)

    assert [r["state"] for r in statuses(on_new)] == [STATE_CONFIRMING, STATE_ROLLING_BACK]
    assert stage.reboot.is_set()
    assert stage.identity.fw_version == "1.4.2"
    stage.reboot.clear()

    back = FakeClient()
    await one_session(stage, back)
    rolled = statuses(back)
    assert [r["state"] for r in rolled] == [STATE_ROLLED_BACK]
    detail = rolled[0]["detail"]
    assert detail == "selftest failed: simulated self-test failure; returned to ota_0"
    assert detail == f"{SELFTEST_FAILED}{SELFTEST_FAIL_REASON}{RETURNED_TO}ota_0"
    assert len(detail.encode()) == 63
    assert parse_selftest_detail(detail) == SelfTestOutcome("failed", SELFTEST_FAIL_REASON, "ota_0")
    assert stage.rolled_back is None and stage.rolled_back_head is None


async def test_a_hung_selftest_reports_timed_out(downloads: list[str]) -> None:
    _ = downloads
    stage = await applied_stage(
        version="1.5.0-hang",
        capabilities=SELFTEST_CAPS,
        broken_marker="-hang",
        selftest_fail_marker="-hang",
        confirm_timeout_s=0.2,
    )
    on_new = FakeClient()
    await one_session(stage, on_new, awake_s=None)
    assert [r["state"] for r in statuses(on_new)] == [STATE_CONFIRMING, STATE_ROLLING_BACK]
    stage.reboot.clear()

    back = FakeClient()
    await one_session(stage, back)
    detail = statuses(back)[0]["detail"]
    assert detail == f"{SELFTEST_TIMED_OUT}{RETURNED_TO}ota_0"
    assert parse_selftest_detail(detail) == SelfTestOutcome("timed_out", None, "ota_0")


async def test_a_board_without_a_selftest_keeps_the_legacy_rollback_detail(
    downloads: list[str],
) -> None:
    _ = downloads
    stage = await applied_stage(
        version="1.5.0-rbtest", broken_marker="-rbtest", confirm_timeout_s=0.2
    )
    await one_session(stage, FakeClient(), awake_s=None)
    stage.reboot.clear()

    back = FakeClient()
    await one_session(stage, back)
    assert statuses(back)[0]["detail"] == "returned to 1.4.2; the new image did not confirm"
    assert parsed_details(back) == [None]


async def test_the_slot_flips_on_an_apply_and_back_on_a_rollback(downloads: list[str]) -> None:
    _ = downloads
    stage = await applied_stage(capabilities=SELFTEST_CAPS, selftest_fail_marker="-stfail")
    assert stage.slot == "ota_1"
    await one_session(stage, FakeClient())
    assert stage.pending is None and stage.slot == "ota_1"

    failing = FakeClient()
    failing.inbox.put_nowait(
        FakeMessage(
            "ff/v1/d/a4cf12b3de90/dn/cmd",
            stage_command(cmd_id="cmd-stage-2", version="1.6.0-stfail"),
        )
    )
    await one_session(stage, failing, awake_s=None)
    assert stage.slot == "ota_0"
    stage.reboot.clear()
    await one_session(stage, FakeClient(), awake_s=None)
    assert stage.slot == "ota_1"
    stage.reboot.clear()

    back = FakeClient()
    await one_session(stage, back)
    detail = statuses(back)[0]["detail"]
    assert detail.endswith("returned to ota_1")
    assert parse_selftest_detail(detail) == SelfTestOutcome("failed", SELFTEST_FAIL_REASON, "ota_1")


def test_the_selftest_marker_needs_a_registered_selftest_and_a_value() -> None:
    plain = DeviceIdentity(device_id="a4cf12b3de90")
    registered = DeviceIdentity(device_id="a4cf12b3de90", capabilities=SELFTEST_CAPS)
    with pytest.raises(SimulatorError, match="selftest"):
        StageRunner(identity=plain, link=LinkProfile(LINK_FAST), selftest_fail_marker="-stfail")
    with pytest.raises(SimulatorError):
        StageRunner(identity=registered, link=LinkProfile(LINK_FAST), selftest_fail_marker="")


def test_the_cli_takes_the_selftest_fail_marker_with_an_equals_sign() -> None:
    args = cli.build_parser().parse_args(
        ["run", "--capabilities", "selftest", "--selftest-fail-marker=-stfail"]
    )
    assert args.selftest_fail_marker == "-stfail"
    assert cli.build_parser().parse_args(["fleet"]).selftest_fail_marker is None

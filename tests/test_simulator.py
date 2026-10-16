"""The device simulator, with **no broker, no API and no database**.

Same split as `test_broker_dynsec.py`: everything provable without I/O is proved here
against a fake `client_factory`, and the live end-to-end check is a human running
`just sim` against `just up` (the acceptance criteria of R0-test-1).

Four of these are not plumbing tests and should not be deleted to make a refactor
pass:

* **the QoS/retain matrix** — a wrong retain flag on `up/hb` is a *silent* wrong
  answer: `ingestor/handlers.py` treats retained messages as replay, so `last_seen`
  would simply stop advancing and every board would drift offline;
* **the will** — retained `{"online":false}`, which is invisible until a board dies;
* **import purity** — a simulator that imports the server's parsing agrees with the
  server by construction and therefore proves nothing about the protocol;
* **secret hygiene** — `mqtt_password` exists in exactly two places (the enroll
  response and a 0600 file) and must reach neither stdout nor the logs.
"""

import ast
import asyncio
import hashlib
import json
import os
import stat
import time
from collections.abc import AsyncIterator
from pathlib import Path
from typing import Any

import pytest

from fleetforge.api.schemas import EnrollRequest
from fleetforge.broker.commands import VERB_COMMANDS as SERVER_VERB_COMMANDS
from fleetforge.db.models import TERMINAL_DEPLOY_STATES, AckResult, DeployState, PowerClass
from fleetforge.identity import DEVICE_ID_RE
from fleetforge.ingestor.protocol import (
    AnnouncePayload,
    PresencePayload,
    UpChannel,
    parse_up_topic,
)
from fleetforge.selftest_outcome import SelfTestOutcome, parse_selftest_detail, selftest_outcome
from fleetforge.simulator import __main__ as cli
from fleetforge.simulator.device import (
    ACK,
    ACK_ACCEPTED,
    ACK_DEFERRED,
    ACK_DETAILS,
    ACK_REFUSED,
    ANNOUNCE,
    CAPABILITY_SELFTEST,
    CONFIRM_AUTO,
    CONFIRM_NEVER,
    CONFIRM_WALK,
    DEFAULT_CONFIRM_TIMEOUT_S,
    DETAIL_APPLY_WAITS,
    DETAIL_ROLLBACK_WAITS,
    HEARTBEAT,
    LINK_FAST,
    LINK_SLOW,
    POWER_CLASSES,
    PRESENCE,
    PRESENCE_OFFLINE,
    PRESENCE_ONLINE,
    QOS,
    RETURNED_TO,
    ROLLBACK_WALK,
    ROLLED_BACK_BY_COMMAND,
    SAFE_WINDOW_AUTO,
    SAFE_WINDOW_HOLD,
    SELFTEST_FAIL_REASON,
    SELFTEST_FAILED,
    SELFTEST_PASSED,
    SELFTEST_TIMED_OUT,
    SLOW_MAX_DELAY_S,
    SLOW_MIN_DELAY_S,
    STAGE_WALK,
    STATE_APPLYING,
    STATE_AWAITING_SAFE_WINDOW,
    STATE_CANCELLED,
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
    VERB_COMMANDS,
    DeviceIdentity,
    LinkProfile,
    PendingConfirm,
    StageRunner,
    derive_device_id,
    dn_filter,
    mqtt_client_factory,
    redact_url,
    redacted_command,
    run_always_on,
    run_session,
    run_sleepy,
    up_topic,
    will_for,
)
from fleetforge.simulator.errors import SimulatorError
from fleetforge.simulator.state import Credential, describe, forget, load, save, state_path
from tests.conftest import capture_logs

SIMULATOR_DIR = Path(__file__).resolve().parent.parent / "src" / "fleetforge" / "simulator"

# A sentinel that could not plausibly appear in a transcript by coincidence.
SECRET = "brk-pw-2f9d1c-DO-NOT-LOG"  # noqa: S105 - a fixture value, not a credential


def credential_for(device_id: str, password: str = SECRET) -> Credential:
    return Credential(
        device_id=device_id,
        mqtt_username=device_id,
        mqtt_password=password,
        api_base="http://localhost:8080",
        enrolled_at="2026-01-01T00:00:00Z",
    )


class FakeMessage:
    """The two attributes `_command_loop` reads off an `aiomqtt.Message`."""

    def __init__(self, topic: str, payload: bytes) -> None:
        self.topic = type("Topic", (), {"value": topic})()
        self.payload = payload


class FakeClient:
    """A recording stand-in for `aiomqtt.Client`.

    Only what `run_session` touches: the async context manager, `subscribe`,
    `publish`, and a `messages` iterator that blocks forever (a real board's `dn/#` is
    silent almost all the time).
    """

    def __init__(self) -> None:
        self.published: list[tuple[str, bytes, int, bool]] = []
        self.subscribed: list[tuple[str, int]] = []
        # A single ordered log, so "subscribe happened before the first publish" is a
        # provable claim rather than an inference from two separate lists.
        self.calls: list[str] = []
        self.enters = 0
        self.inbox: asyncio.Queue[FakeMessage] = asyncio.Queue()
        # `aiomqtt.Client.messages` is an attribute holding an async iterator, not a
        # coroutine — `_command_loop` does `async for message in client.messages`.
        self.messages = _drain(self.inbox)

    async def __aenter__(self) -> "FakeClient":
        self.enters += 1
        return self

    async def __aexit__(self, *_exc: object) -> bool:
        return False

    async def subscribe(self, topic: str, qos: int = 0) -> None:
        self.subscribed.append((topic, qos))
        self.calls.append(f"subscribe {topic}")

    async def publish(
        self, topic: str, payload: bytes = b"", qos: int = 0, retain: bool = False
    ) -> None:
        self.published.append((topic, bytes(payload), qos, retain))
        self.calls.append(f"publish {topic}")


async def _drain(inbox: "asyncio.Queue[FakeMessage]") -> AsyncIterator[FakeMessage]:
    while True:
        yield await inbox.get()


def transcript() -> tuple[list[str], Any]:
    """A `step` sink plus the list it appends to."""
    lines: list[str] = []
    return lines, lines.append


async def drive_session(
    device: DeviceIdentity,
    *,
    awake_s: float = 0.05,
    goodbye: bool = True,
    step: Any = None,
    client: FakeClient | None = None,
) -> FakeClient:
    """Run one full session against a `FakeClient` and return it."""
    fake = client or FakeClient()
    await run_session(
        device,
        credential_for(device.device_id),
        client_factory=lambda: fake,  # type: ignore[arg-type,return-value]
        link=LinkProfile(LINK_FAST),
        heartbeat_interval_s=0.01,
        awake_s=awake_s,
        goodbye=goodbye,
        step=step or (lambda _message: None),
    )
    return fake


# ---------------------------------------------------------------------------
# Identity
# ---------------------------------------------------------------------------


@pytest.mark.parametrize("name", ["blinker", "sim", "sim-01", "", "Ünïcødé", "a" * 200])
def test_derived_ids_are_valid_locally_administered_unicast_macs(name: str) -> None:
    """The id must satisfy `DEVICE_ID_RE` and be provably not a real eFuse MAC.

    Bit 1 of the first octet set = locally administered; bit 0 clear = unicast. An
    Espressif MAC is neither, so a simulated board can never collide with hardware —
    and because 0xff is odd, a derived id can never begin `ff`, which keeps
    `broker/__main__.py`'s reserved `ffff…` selftest ids reserved.
    """
    device_id = derive_device_id(name)
    assert DEVICE_ID_RE.match(device_id)
    first = int(device_id[:2], 16)
    assert first & 0x02, "not locally administered"
    assert not first & 0x01, "not unicast"
    assert not device_id.startswith("ff")


def test_derived_ids_are_stable_and_distinct() -> None:
    """`just sim --name blinker` must be the same board in the dashboard every time."""
    assert derive_device_id("blinker") == derive_device_id("blinker")
    ids = {derive_device_id(f"sim-{n:02d}") for n in range(50)}
    assert len(ids) == 50


def test_power_classes_agree_with_the_database_model() -> None:
    """`POWER_CLASSES` is retyped, not imported (import purity) — so it needs a tripwire."""
    assert set(POWER_CLASSES) == {member.value for member in PowerClass}


def test_validate_rejects_what_the_server_would_reject_before_any_io() -> None:
    """Every one of these would otherwise cost a single-use enrollment token."""
    with pytest.raises(SimulatorError, match="12 lowercase hex"):
        DeviceIdentity(device_id="A4CF12B3DE90").validate()
    with pytest.raises(SimulatorError, match="12 lowercase hex"):
        DeviceIdentity(device_id="nothex").validate()
    with pytest.raises(SimulatorError, match="power_class"):
        DeviceIdentity(device_id="a4cf12b3de90", power_class="mains").validate()
    with pytest.raises(SimulatorError, match="wake-interval"):
        DeviceIdentity(device_id="a4cf12b3de90", power_class="sleepy").validate()
    # And the happy paths stay happy.
    DeviceIdentity(device_id="a4cf12b3de90").validate()
    DeviceIdentity(
        device_id="a4cf12b3de90", power_class="sleepy", expected_wake_interval_s=300
    ).validate()


def test_bad_identity_never_reaches_the_network(monkeypatch: pytest.MonkeyPatch) -> None:
    """`main()` refuses the identity, prints one line, and spends no token."""

    async def explode(*_args: object, **_kwargs: object) -> None:
        raise AssertionError("enroll() must not be reached for an invalid identity")

    monkeypatch.setattr(cli, "enroll", explode)
    code = cli.main(
        ["run", "--name", "x", "--device-id", "NOPE", "--token", "ffe_x", "--state-dir", "/nope"]
    )
    assert code == 1


# ---------------------------------------------------------------------------
# Payloads — the spec's key sets, validated by the server's own models
# ---------------------------------------------------------------------------

ANNOUNCE_KEYS = {
    "proto",
    "device_id",
    "platform_type",
    "fw_version",
    "agent_version",
    "link_type",
    "ssid",
    "known_networks",
    "power_class",
    "expected_wake_interval_s",
    "parent_device_id",
    "partition_layout",
    "ota_slot_size",
    "flash_chip_size",
    "partition_table_sha256",
    "rollback_capable",
    "capabilities",
    "reset_reason",
}


def test_announce_matches_the_spec_key_set_and_carries_no_timestamp() -> None:
    """`spec/device-protocol.md`: the server timestamps by its own receipt time."""
    body = DeviceIdentity(device_id="a4cf12b3de90").announce()
    assert set(body) == ANNOUNCE_KEYS
    assert "ts" not in body
    assert "timestamp" not in body


def test_announce_decodes_through_the_ingestors_own_model() -> None:
    """The bytes on the wire must be what `ingestor/protocol.py` will actually read."""
    device = DeviceIdentity(device_id="a4cf12b3de90", capabilities=("ota",))
    parsed = AnnouncePayload.model_validate(json.loads(json.dumps(device.announce())))
    assert parsed.device_id == "a4cf12b3de90"
    assert parsed.power_class == "always_on"
    assert parsed.capabilities == ["ota"]


def test_announce_carries_the_network_in_spec_order() -> None:
    """Agent 0.4.6: `ssid`, `known_networks` right after `link_type`, null when unset."""
    device = DeviceIdentity(device_id="a4cf12b3de90", ssid="shed", known_networks=2)
    body = device.announce()
    keys = list(body)
    link = keys.index("link_type")
    assert keys[link : link + 3] == ["link_type", "ssid", "known_networks"]

    parsed = AnnouncePayload.model_validate(json.loads(json.dumps(body)))
    assert (parsed.ssid, parsed.known_networks) == ("shed", 2)

    bare = DeviceIdentity(device_id="a4cf12b3de90").announce()
    assert (bare["ssid"], bare["known_networks"]) == (None, None)


def test_announce_carries_reset_reason_after_capabilities() -> None:
    """S0-fw-6: `poweron` on the first start, after `capabilities`, as the spec prints it."""
    body = DeviceIdentity(device_id="a4cf12b3de90").announce()
    keys = list(body)
    assert keys[keys.index("capabilities") + 1] == "reset_reason"
    assert body["reset_reason"] == "poweron"
    parsed = AnnouncePayload.model_validate(json.loads(json.dumps(body)))
    assert parsed.reset_reason == "poweron"


MEASUREMENTS = ["flash_chip_size", "partition_table_sha256", "rollback_capable"]


def test_announce_carries_the_board_measurements_in_spec_order() -> None:
    """R2b-be-6: after `ota_slot_size`, before `capabilities`; null when unset."""
    sha = "1fa67e6bbd034e434d04e9d6f4f52bbe899361602cd498573eb3bde97d1559ed"
    device = DeviceIdentity(
        device_id="a4cf12b3de90",
        flash_chip_size=4194304,
        partition_table_sha256=sha,
        rollback_capable=False,
    )
    body = device.announce()
    keys = list(body)
    slot = keys.index("ota_slot_size")
    assert keys[slot : slot + 5] == ["ota_slot_size", *MEASUREMENTS, "capabilities"]

    parsed = AnnouncePayload.model_validate(json.loads(json.dumps(body)))
    assert (parsed.flash_chip_size, parsed.partition_table_sha256, parsed.rollback_capable) == (
        4194304,
        sha,
        False,
    )

    bare = DeviceIdentity(device_id="a4cf12b3de90").announce()
    assert [bare[key] for key in MEASUREMENTS] == [None, None, None]


@pytest.mark.parametrize(
    ("argv", "expected"),
    [
        ([], (None, None, None)),
        (["--rollback-capable", "true"], (None, None, True)),
        (["--rollback-capable", "false"], (None, None, False)),
        (["--flash-chip-size", "4194304", "--partition-sha", "abc"], (4194304, "abc", None)),
    ],
)
def test_the_cli_announces_measurements_only_when_told(
    argv: list[str], expected: tuple[int | None, str | None, bool | None]
) -> None:
    """No flag is null, as every agent <= 0.4.6 sends; junk is passed through verbatim."""
    args = cli.build_parser().parse_args(["run", *argv])
    identity = cli._identity_for(args, "a4cf12b3de90")
    assert (
        identity.flash_chip_size,
        identity.partition_table_sha256,
        identity.rollback_capable,
    ) == expected


def test_the_cli_refuses_a_rollback_capable_that_is_not_true_or_false() -> None:
    with pytest.raises(SystemExit):
        cli.build_parser().parse_args(["run", "--rollback-capable", "maybe"])


@pytest.mark.parametrize(
    ("argv", "expected"),
    [
        ([], ("sim-wifi", 1)),
        (["--ssid", "shed", "--known-networks", "2"], ("shed", 2)),
        (["--known-networks", "0"], ("sim-wifi", 0)),
        (["--link-type", "ethernet"], (None, None)),
        (["--link-type", "ethernet", "--ssid", "odd", "--known-networks", "3"], ("odd", 3)),
    ],
)
def test_the_cli_announces_a_network_on_wifi_and_none_on_ethernet(
    argv: list[str], expected: tuple[str | None, int | None]
) -> None:
    args = cli.build_parser().parse_args(["run", *argv])
    identity = cli._identity_for(args, "a4cf12b3de90")
    assert (identity.ssid, identity.known_networks) == expected


@pytest.mark.parametrize(
    "device",
    [
        DeviceIdentity(device_id="a4cf12b3de90"),
        DeviceIdentity(
            device_id="a4cf12b3de91", power_class="sleepy", expected_wake_interval_s=300
        ),
        DeviceIdentity(device_id="a4cf12b3de92", ssid="shed", known_networks=2),
        DeviceIdentity(
            device_id="a4cf12b3de93",
            flash_chip_size=4194304,
            partition_table_sha256="1fa67e6bbd034e434d04e9d6f4f52bbe899361602cd498573eb3bde97d1559ed",
            rollback_capable=True,
        ),
    ],
)
def test_enroll_body_is_flat_and_validates_against_enrollrequest(device: DeviceIdentity) -> None:
    """A nested `{"token": …, "identity": {…}}` body would be a recall (schemas.py)."""
    body = device.enroll_body("ffe_abc")
    assert body["token"] == "ffe_abc"
    assert not any(isinstance(value, dict) for value in body.values()), "the body must stay flat"
    parsed = EnrollRequest.model_validate(body)
    assert parsed.device_id == device.device_id
    assert parsed.power_class == device.power_class
    assert (parsed.ssid, parsed.known_networks) == (device.ssid, device.known_networks)
    assert (parsed.flash_chip_size, parsed.partition_table_sha256, parsed.rollback_capable) == (
        device.flash_chip_size,
        device.partition_table_sha256,
        device.rollback_capable,
    )


def test_heartbeat_body_is_the_spec_body() -> None:
    body = DeviceIdentity(device_id="a4cf12b3de90").heartbeat(41)
    assert set(body) == {
        "fw_version",
        "uptime_s",
        "rssi",
        "free_heap",
        "min_free_heap",
        "boot_ok",
    }
    assert body["uptime_s"] == 41


# ---------------------------------------------------------------------------
# Topics
# ---------------------------------------------------------------------------


@pytest.mark.parametrize("channel", [ANNOUNCE, PRESENCE, HEARTBEAT])
def test_up_topics_round_trip_through_the_ingestors_parser(channel: str) -> None:
    parsed = parse_up_topic(up_topic("a4cf12b3de90", channel))
    assert parsed is not None
    assert parsed.device_id == "a4cf12b3de90"
    assert parsed.channel == channel


def test_dn_filter_is_the_pattern_acl_the_broker_grants() -> None:
    """`mosquitto/acl` grants `ff/v1/d/%u/dn/#` and nothing wider."""
    assert dn_filter("a4cf12b3de90") == "ff/v1/d/a4cf12b3de90/dn/#"
    # A `dn/` topic is not an `up/` topic, and the ingestor must not see it as one.
    assert parse_up_topic("ff/v1/d/a4cf12b3de90/dn/cmd") is None


# ---------------------------------------------------------------------------
# The session: QoS, retain and ordering
# ---------------------------------------------------------------------------


async def test_session_publishes_the_spec_qos_and_retain_matrix() -> None:
    """announce retained, presence retained, **hb never retained**, everything QoS 1."""
    device = DeviceIdentity(device_id="a4cf12b3de90")
    fake = await drive_session(device)

    by_topic: dict[str, list[tuple[bytes, int, bool]]] = {}
    for topic, payload, qos, retain in fake.published:
        by_topic.setdefault(topic, []).append((payload, qos, retain))

    announce = by_topic[up_topic(device.device_id, ANNOUNCE)]
    presence = by_topic[up_topic(device.device_id, PRESENCE)]
    heartbeats = by_topic[up_topic(device.device_id, HEARTBEAT)]

    assert all(qos == QOS for _, _, qos, _ in fake.published), "every publish is QoS 1"
    assert [retain for _, _, retain in announce] == [True]
    assert presence[0] == (PRESENCE_ONLINE, 1, True)
    assert heartbeats, "the board must have beaten at least once"
    assert not any(retain for _, _, retain in heartbeats), (
        "a retained hb is replayed on every reconnect and the ingestor treats a replay "
        "as not-live, so last_seen would silently stop advancing"
    )
    assert PresencePayload.model_validate(json.loads(presence[0][0])).online is True


async def test_session_subscribes_before_it_publishes_and_announces_before_presence() -> None:
    """Ordering is the contract: queued `dn/` commands are drained before anything else."""
    device = DeviceIdentity(device_id="a4cf12b3de90")
    fake = await drive_session(device)

    assert fake.calls[0] == f"subscribe {dn_filter(device.device_id)}"
    assert fake.subscribed == [(dn_filter(device.device_id), QOS)]
    publishes = [call for call in fake.calls if call.startswith("publish ")]
    assert publishes[0] == f"publish {up_topic(device.device_id, ANNOUNCE)}"
    assert publishes[1] == f"publish {up_topic(device.device_id, PRESENCE)}"


async def test_a_clean_stop_publishes_the_retained_goodbye() -> None:
    """A clean DISCONNECT does not fire the LWT, so the board must say goodbye itself."""
    device = DeviceIdentity(device_id="a4cf12b3de90")
    fake = await drive_session(device)
    assert fake.published[-1] == (up_topic(device.device_id, PRESENCE), PRESENCE_OFFLINE, 1, True)


async def test_a_sleepy_wake_does_not_say_goodbye() -> None:
    """For a sleepy board `presence_reported` is ignored — a goodbye would be noise."""
    device = DeviceIdentity(
        device_id="a4cf12b3de90", power_class="sleepy", expected_wake_interval_s=20
    )
    fake = await drive_session(device, goodbye=False)
    assert PRESENCE_OFFLINE not in [payload for _, payload, _, _ in fake.published]


async def test_the_session_stops_when_stop_is_set() -> None:
    """`--duration` and SIGINT both work by setting this event, then the goodbye runs."""
    device = DeviceIdentity(device_id="a4cf12b3de90")
    fake = FakeClient()
    stop = asyncio.Event()

    async def stopper() -> None:
        await asyncio.sleep(0.05)
        stop.set()

    async with asyncio.timeout(5):
        await asyncio.gather(
            run_session(
                device,
                credential_for(device.device_id),
                client_factory=lambda: fake,  # type: ignore[arg-type,return-value]
                link=LinkProfile(LINK_FAST),
                heartbeat_interval_s=0.01,
                stop=stop,
            ),
            stopper(),
        )
    assert fake.enters == 1
    assert fake.published[-1][3] is True


async def test_queued_commands_are_logged_once_and_duplicates_dropped() -> None:
    """QoS 1 is at-least-once; `spec/device-protocol.md` makes dedup the device's job."""
    device = DeviceIdentity(device_id="a4cf12b3de90")
    fake = FakeClient()
    body = json.dumps({"id": "cmd-1", "type": "ping"}).encode()
    topic = f"ff/v1/d/{device.device_id}/dn/cmd"
    fake.inbox.put_nowait(FakeMessage(topic, body))
    fake.inbox.put_nowait(FakeMessage(topic, body))
    lines, step = transcript()

    await drive_session(device, awake_s=0.15, step=step, client=fake)

    commands = [line for line in lines if line.startswith("command ")]
    assert any("id=cmd-1 type=ping" in line for line in commands)
    assert any("DUPLICATE" in line for line in commands)
    # R1-be-2 executes `stage` and nothing else; the rest of the vocabulary is R2.
    assert any("not implemented" in line for line in commands)


async def test_an_undecodable_command_does_not_kill_the_session() -> None:
    device = DeviceIdentity(device_id="a4cf12b3de90")
    fake = FakeClient()
    fake.inbox.put_nowait(FakeMessage(f"ff/v1/d/{device.device_id}/dn/cmd", b"\xff not json"))
    lines, step = transcript()

    await drive_session(device, awake_s=0.15, step=step, client=fake)

    assert any("undecodable" in line for line in lines)
    assert any(line.startswith("goodbye") for line in lines)


# ---------------------------------------------------------------------------
# Executing a `stage` (R1-be-2)
# ---------------------------------------------------------------------------

FIRMWARE = b"\xe9\x06\x02\x20" + b"firmware bytes, opaque to the board" * 29
FIRMWARE_SHA256 = hashlib.sha256(FIRMWARE).hexdigest()
# Shaped like a real signed URL: the redaction test is only meaningful if there is a
# query string that must not survive it.
SIGNED_URL = (
    f"https://storage.invalid/blobs/sha256/{FIRMWARE_SHA256}"
    "?X-Goog-Signature=deadbeefdeadbeef&X-Goog-Expires=1800"
)


def stage_command(
    *,
    cmd_id: str = "cmd-stage-1",
    url: str = SIGNED_URL,
    sha256: str = FIRMWARE_SHA256,
    version: str = "1.5.0",
    apply: str = "auto",
    size: int = len(FIRMWARE),
) -> bytes:
    """The `stage` body exactly as `api/routers/deploys.py` publishes it.

    Retyped rather than imported: import purity forbids `fleetforge.broker` here, and a
    simulator test that built its input with the server's builder would prove nothing
    about the two agreeing.
    """
    return json.dumps(
        {
            "id": cmd_id,
            "type": "stage",
            "artifact": {
                "url": url,
                "sha256": sha256,
                "size": size,
                "version": version,
            },
            "apply": apply,
            "confirm_timeout_s": 300,
        }
    ).encode()


class FakeHttpResponse:
    """The three things `_fetch` uses: a context manager and a chunked `read`."""

    def __init__(self, data: bytes) -> None:
        self._data = data
        self._offset = 0

    def __enter__(self) -> "FakeHttpResponse":
        return self

    def __exit__(self, *_exc: object) -> bool:
        return False

    def read(self, size: int = -1) -> bytes:
        chunk = self._data[self._offset :] if size < 0 else self._data[self._offset :][:size]
        self._offset += len(chunk)
        return chunk


@pytest.fixture
def downloads(monkeypatch: pytest.MonkeyPatch) -> list[str]:
    """Record every URL fetched and serve `FIRMWARE`. One entry per real download."""
    fetched: list[str] = []

    def fake_urlopen(url: str, timeout: float | None = None) -> FakeHttpResponse:
        fetched.append(url)
        return FakeHttpResponse(FIRMWARE)

    monkeypatch.setattr("urllib.request.urlopen", fake_urlopen)
    return fetched


async def run_stage(
    body: bytes,
    *,
    safe_window: str = SAFE_WINDOW_AUTO,
    device: DeviceIdentity | None = None,
    repeats: int = 1,
    awake_s: float = 0.3,
) -> tuple[StageRunner, FakeClient, list[str]]:
    """Deliver `body` (`repeats` times) to one session and return what happened."""
    identity = device or DeviceIdentity(device_id="a4cf12b3de90", fw_version="1.4.2")
    fake = FakeClient()
    for _ in range(repeats):
        fake.inbox.put_nowait(FakeMessage(f"ff/v1/d/{identity.device_id}/dn/cmd", body))
    stage = StageRunner(identity=identity, link=LinkProfile(LINK_FAST), safe_window=safe_window)
    lines, step = transcript()
    await run_session(
        identity,
        credential_for(identity.device_id),
        client_factory=lambda: fake,  # type: ignore[arg-type,return-value]
        link=LinkProfile(LINK_FAST),
        heartbeat_interval_s=10.0,
        awake_s=awake_s,
        stage=stage,
        step=step,
    )
    return stage, fake, lines


def statuses(fake: FakeClient, device_id: str = "a4cf12b3de90") -> list[dict[str, Any]]:
    return [
        json.loads(payload)
        for topic, payload, _, _ in fake.published
        if topic == up_topic(device_id, STATUS)
    ]


def test_the_retyped_states_are_the_servers_states() -> None:
    """Import purity means these are copies; `DeployState` is the original."""
    values = {state.value for state in DeployState}
    for state in (*STAGE_WALK, STATE_AWAITING_SAFE_WINDOW, STATE_FAILED):
        assert state in values, f"{state} is not a DeployState"
    assert STATE_STAGED in values and STATE_VERIFYING in values
    # R2-be-1: the outcome, reported by the session after the reboot.
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
        # 65 characters. The firmware's `take_string` sees this as too long for its
        # buffer and says "malformed" too (R2-fw-1), so the two devices agree.
        FIRMWARE_SHA256 + "0",
        "g" * 64,
    ],
)
async def test_a_malformed_digest_is_refused_before_any_download(
    downloads: list[str], sha256: str
) -> None:
    """R2-fw-1: the firmware refuses a misspelt digest at the command seam, before any I/O
    — never normalised. The simulator is a device, so it refuses the same command."""
    _, fake, _ = await run_stage(stage_command(sha256=sha256))

    reports = statuses(fake)
    assert [report["state"] for report in reports] == [STATE_FAILED]
    assert reports[-1]["detail"] == "artifact sha256 malformed"
    assert downloads == []


async def test_an_artifact_larger_than_the_slot_is_refused_before_download(
    downloads: list[str],
) -> None:
    """R2-fw-1: one byte over the 1966080-byte slot. `staging` first, because the firmware
    learns the slot size only after publishing it; nothing is fetched."""
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
    _, fake, _ = await run_stage(stage_command(apply="on_command"))

    states = [report["state"] for report in statuses(fake)]
    assert states[-1] == STATE_STAGED
    assert STATE_APPLYING not in states


async def test_the_board_comes_back_running_the_new_version(downloads: list[str]) -> None:
    stage, _, _ = await run_stage(stage_command(version="1.5.0"))

    assert stage.identity.fw_version == "1.5.0"
    assert stage.downloads == 1


async def test_the_transcript_prints_the_command_with_the_url_redacted(
    downloads: list[str],
) -> None:
    """The only place a human can see a `dn/` payload — and it is a bearer credential."""
    _, _, lines = await run_stage(stage_command())

    joined = "\n".join(lines)
    assert "X-Goog-Signature" not in joined
    assert SIGNED_URL not in joined
    assert "https://storage.invalid/…" in joined
    # Everything else about the command is printed, or the line would be useless.
    assert '"type": "stage"' in joined
    assert FIRMWARE_SHA256 in joined


# ---------------------------------------------------------------------------
# The reboot after an apply (S0-test-4)
#
# The defect these cover is the reason CUJ-1 segment 5 could not pass: `_stage`
# rebound `StageRunner.identity` and stopped there, but `run_session` had captured the
# old frozen identity and its heartbeat loop kept publishing from it. An `always_on`
# board never dropped the session, so the "next connect" the apply line promises never
# came and `GET /v1/devices` reported the old `fw_version` forever. Everything below
# asserts convergence rather than the rebind — the rebind was always correct and always
# invisible.
#
# These are also the only tests that drive the reconnect loops at all.
# ---------------------------------------------------------------------------


def channel_payloads(fake: FakeClient, device_id: str, channel: str) -> list[dict[str, Any]]:
    """Every JSON body this session published on one `up/` channel, in order."""
    return [
        json.loads(payload)
        for topic, payload, _, _ in fake.published
        if topic == up_topic(device_id, channel)
    ]


def session_factory(
    device_id: str, first_command: bytes | None = None
) -> tuple[list[FakeClient], Any]:
    """A `client_factory` handing out a **fresh** `FakeClient` per session.

    Fresh because the real one is (property 5), and because "the board reconnected" is
    otherwise unobservable: the list of clients *is* the list of sessions. Only the
    first session receives the command, exactly like a broker that delivered a `stage`
    once.
    """
    clients: list[FakeClient] = []

    def factory() -> FakeClient:
        fake = FakeClient()
        if not clients and first_command is not None:
            fake.inbox.put_nowait(FakeMessage(f"ff/v1/d/{device_id}/dn/cmd", first_command))
        clients.append(fake)
        return fake

    return clients, factory


async def wait_until(predicate: Any, timeout: float = 5.0) -> None:
    """Poll until `predicate()`, or fail the test rather than hang the suite."""
    deadline = time.monotonic() + timeout
    while not predicate():
        if time.monotonic() > deadline:
            raise AssertionError("the simulator never reached the expected state")
        await asyncio.sleep(0.01)


async def drive_until(runner: Any, predicate: Any, stop: asyncio.Event) -> None:
    """Run a forever-loop (`run_always_on` / `run_sleepy`) until `predicate`, then stop."""
    task = asyncio.create_task(runner)
    try:
        await wait_until(predicate)
    finally:
        stop.set()
        await asyncio.wait_for(task, timeout=5.0)


async def test_an_apply_ends_the_session_and_the_next_one_reports_the_new_version(
    downloads: list[str], monkeypatch: pytest.MonkeyPatch
) -> None:
    """S0-test-4, the whole of it: `fw_version` converges without anyone touching it."""
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
    # Both, deliberately: the ingestor reads `fw_version` off the **heartbeat**
    # (`ingestor/store.py`), which is the message that carried the stale version for as
    # long as this defect was open, and the announce is what a dashboard row is built from.
    assert channel_payloads(after, device_id, ANNOUNCE)[0]["fw_version"] == "1.6.0"
    assert channel_payloads(after, device_id, HEARTBEAT)[0]["fw_version"] == "1.6.0"
    # S0-fw-6: the first start is a power-on, the apply reboot an `esp_restart()`.
    assert channel_payloads(before, device_id, ANNOUNCE)[0]["reset_reason"] == "poweron"
    assert channel_payloads(after, device_id, ANNOUNCE)[0]["reset_reason"] == "sw"
    # The last thing the old session said was `rebooting`, and it did not say goodbye:
    # a restarting board stops mid-sentence, it does not report itself offline.
    assert [report["state"] for report in statuses(before)] == list(STAGE_WALK)
    assert PRESENCE_OFFLINE not in [payload for _, payload, _, _ in before.published]
    assert any(line.startswith("reboot") for line in lines)
    # R2-be-1: the session on the new image reports the outcome of the same transaction.
    await wait_until(lambda: len(statuses(after)) == len(CONFIRM_WALK))
    assert [report["state"] for report in statuses(after)] == list(CONFIRM_WALK)
    assert {report["cmd_id"] for report in statuses(after)} == {"cmd-stage-1"}


async def test_an_apply_with_no_version_still_reboots(
    downloads: list[str], monkeypatch: pytest.MonkeyPatch
) -> None:
    """The reboot is the consequence of the apply, not of the payload naming a version."""
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
    """Same convergence, one duty cycle later — the acceptance criterion's second half.

    Honest about what it proves: a sleepy board ends its session every `awake_s`
    regardless, so this one **passes even with the reboot removed**. It is a regression
    guard for the half of the fleet that was accidentally fine, not a reproduction of
    the defect — `test_an_apply_ends_the_session_…` is the test that fails without the
    fix.
    """
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
    # `apply=on_command` stages and waits for R2; no apply, so no reboot.
    _, fake, _ = await run_stage(stage_command(apply="on_command"), awake_s=0.15)

    assert fake.enters == 1
    assert any(payload == PRESENCE_OFFLINE for _, payload, _, _ in fake.published), (
        "a session that ended without applying anything still owes its goodbye"
    )


# ---------------------------------------------------------------------------
# The outcome after the reboot (R2-be-1)
#
# The session on the image the board rebooted into reports `confirming` → `confirmed`;
# a `--confirm never` image reports `confirming` → `rolling_back`, goes back, and the
# session on the image it RETURNED to reports `rolled_back`. Same split as the firmware
# (`agent/components/fleetforge/src/ff_mqtt.c`): the outcome is reported by whoever observed it.
# ---------------------------------------------------------------------------


async def one_session(
    stage: StageRunner,
    fake: FakeClient,
    *,
    awake_s: float | None = 0.1,
    stop: asyncio.Event | None = None,
) -> list[str]:
    """Run exactly one session of `stage`'s board against `fake`; return its transcript."""
    lines, step = transcript()
    await asyncio.wait_for(
        run_session(
            stage.identity,
            credential_for(stage.identity.device_id),
            client_factory=lambda: fake,  # type: ignore[arg-type,return-value]
            link=LinkProfile(LINK_FAST),
            heartbeat_interval_s=10.0,
            awake_s=awake_s,
            stop=stop,
            stage=stage,
            step=step,
        ),
        timeout=5.0,
    )
    return lines


async def applied_stage(
    *, version: str = "1.5.0", capabilities: tuple[str, ...] = (), **kwargs: Any
) -> StageRunner:
    """A board on 1.4.2 that has just applied `version` (`cmd-stage-1`) and is rebooting."""
    stage = StageRunner(
        identity=DeviceIdentity(
            device_id="a4cf12b3de90", fw_version="1.4.2", capabilities=capabilities
        ),
        link=LinkProfile(LINK_FAST),
        **kwargs,
    )
    first = FakeClient()
    first.inbox.put_nowait(
        FakeMessage("ff/v1/d/a4cf12b3de90/dn/cmd", stage_command(version=version))
    )
    await one_session(stage, first, awake_s=None)
    assert stage.reboot.is_set(), "the apply did not reboot"
    assert [report["state"] for report in statuses(first)] == list(STAGE_WALK)
    stage.reboot.clear()  # what run_always_on does before the next session
    return stage


async def test_a_confirming_image_reports_confirmed_once_and_then_nothing(
    downloads: list[str],
) -> None:
    """`confirming` → `confirmed`, both retained, both for the applied cmd_id, after the
    announce — and the transaction is closed, so the session after says nothing."""
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
    # Announce first: the server knows the running version when the outcome lands.
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
    """The whole rollback, through `run_always_on`: the timer fires, the board comes back
    on the version it left, and THAT session reports `rolled_back`."""
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
    # S0-fw-6: the confirm timer's rollback is an `esp_restart()` too.
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
    # A rollback is a reboot, not a goodbye: the session on the bad image says nothing
    # on its way out.
    assert PRESENCE_OFFLINE not in [payload for _, payload, _, _ in on_new.published]
    assert sum(line.startswith("reboot") for line in lines) >= 2


async def test_stop_during_the_confirm_timer_cancels_it_cleanly(downloads: list[str]) -> None:
    """Ctrl-C while an image waits to be confirmed: no rollback, and the goodbye still goes."""
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
    stage = await applied_stage(confirm=CONFIRM_NEVER, confirm_timeout_s=0.3)
    await one_session(stage, FakeClient(), awake_s=0.2)  # a blip before the timer fires
    assert stage.pending is not None

    second = FakeClient()
    started = time.monotonic()
    await one_session(stage, second, awake_s=None)

    assert time.monotonic() - started < 0.25, "the timer restarted from zero on reconnect"
    assert [report["state"] for report in statuses(second)] == [
        STATE_CONFIRMING,
        STATE_ROLLING_BACK,
    ]
    assert stage.reboot.is_set() and stage.rolled_back == "cmd-stage-1"


# ---------------------------------------------------------------------------
# R4-fw-1: `up/hb.boot_ok` is false only while the image the board rebooted into waits to
# confirm, the firmware's "otadata is PENDING_VERIFY". `one_session` beats once, at start.
# ---------------------------------------------------------------------------


def boot_oks(fake: FakeClient) -> list[object]:
    return [hb["boot_ok"] for hb in channel_payloads(fake, "a4cf12b3de90", HEARTBEAT)]


async def test_a_heartbeat_inside_the_confirm_window_says_boot_ok_false(
    downloads: list[str],
) -> None:
    stage = await applied_stage(confirm=CONFIRM_NEVER, confirm_timeout_s=0.3)
    assert stage.boot_ok is False, "rebooted into an unconfirmed image"

    on_new = FakeClient()
    await one_session(stage, on_new, awake_s=None)  # ends when the confirm timer fires
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
    stage = await applied_stage(confirm=CONFIRM_AUTO)
    assert stage.boot_ok is False, "not confirmed until the announce is acknowledged"

    on_new = FakeClient()
    await one_session(stage, on_new)
    assert [report["state"] for report in statuses(on_new)] == list(CONFIRM_WALK)
    assert channel_payloads(on_new, "a4cf12b3de90", HEARTBEAT)[0]["fw_version"] == "1.5.0"
    assert boot_oks(on_new) == [True]
    assert stage.boot_ok is True


def test_boot_ok_stays_true_on_the_running_image_after_an_apply() -> None:
    """An apply sets `pending` and `reboot` together: the image still running until the
    session ends is the old, confirmed one, as on metal."""
    stage = StageRunner(
        identity=DeviceIdentity(device_id="a4cf12b3de90", fw_version="1.4.2"),
        link=LinkProfile(LINK_FAST),
    )
    assert stage.boot_ok is True
    stage.pending = PendingConfirm(cmd_id="cmd-stage-1", previous_identity=stage.identity)
    stage.reboot.set()
    assert stage.boot_ok is True
    stage.reboot.clear()  # the next session runs the new image
    assert stage.boot_ok is False


# ---------------------------------------------------------------------------
# R2-fw-2: a board never writes a slot the boot pointer names, and never stages over an
# image that has not confirmed. The firmware refuses both before any I/O
# (`ff_ota.c::choose_target_slot`), after `staging`; the simulator is a device, so it
# refuses the same commands, in the same order, with the same details.
# ---------------------------------------------------------------------------


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
    await one_session(stage, second, awake_s=None)  # ends when the confirm timer fires
    return stage, second


async def test_a_stage_while_the_image_is_still_confirming_is_refused(
    downloads: list[str],
) -> None:
    stage, second = await unconfirmed_then_refused()

    refused = for_cmd(second, "cmd-2")
    assert [report["state"] for report in refused] == [STATE_STAGING, STATE_FAILED]
    assert refused[-1]["detail"] == "the running image is not confirmed yet"
    assert len(downloads) == 1, "only the applied image was ever fetched"
    # The refusal changes nothing about the rollback: the timer still fires.
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
    await one_session(stage, third, awake_s=None)  # ends at the apply's reboot

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
    assert DEFAULT_CONFIRM_TIMEOUT_S == 300.0  # spec/prd.md -> Timing


# ---------------------------------------------------------------------------
# R2b-test-3: `--broken-marker`. One board takes a good build (it confirms) and then a
# `-rbtest` build (it never does) in one process, without a second board or a restart.
# ---------------------------------------------------------------------------


async def test_an_image_carrying_the_broken_marker_never_confirms_though_confirm_is_auto(
    downloads: list[str],
) -> None:
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
    stage = await applied_stage(version="1.5.0", broken_marker="-rbtest", confirm_timeout_s=0.2)
    second = FakeClient()
    await one_session(stage, second)

    assert [report["state"] for report in statuses(second)] == list(CONFIRM_WALK)
    assert stage.pending is None and not stage.reboot.is_set()
    assert stage.rolled_back is None


async def test_one_board_confirms_a_good_build_then_rolls_back_a_broken_one(
    downloads: list[str],
) -> None:
    """Good then broken on ONE runner: the rollback lands on the good build's version,
    not the version the board booted with."""
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
    # A bare `--broken-marker -rbtest` is read by argparse as an option: the `=` is required.
    args = cli.build_parser().parse_args(["fleet", "--broken-marker=-rbtest"])
    assert args.broken_marker == "-rbtest"
    assert cli.build_parser().parse_args(["fleet"]).broken_marker is None
    with pytest.raises(SystemExit):
        cli.build_parser().parse_args(["fleet", "--broken-marker", "-rbtest"])


def test_redact_url_keeps_the_origin_and_drops_everything_else() -> None:
    assert redact_url(SIGNED_URL) == "https://storage.invalid/…"
    assert redact_url("not a url") == "<unparseable url>"


def test_redacted_command_leaves_a_command_without_an_artifact_alone() -> None:
    body = {"id": "x", "type": "reboot"}
    assert redacted_command(body) == body


def test_an_unknown_safe_window_mode_is_refused_before_any_io() -> None:
    with pytest.raises(SimulatorError):
        StageRunner(
            identity=DeviceIdentity(device_id="a4cf12b3de90"),
            link=LinkProfile(LINK_FAST),
            safe_window="whenever",
        )


def test_the_cli_offers_both_safe_window_modes() -> None:
    args = cli.build_parser().parse_args(["run", "--safe-window", "hold"])
    assert args.safe_window == SAFE_WINDOW_HOLD
    assert cli.build_parser().parse_args(["run"]).safe_window == SAFE_WINDOW_AUTO


# ---------------------------------------------------------------------------
# The Last Will
# ---------------------------------------------------------------------------


def test_the_will_is_a_retained_offline_presence() -> None:
    """Retained on purpose: a server that restarts must still learn the board is gone."""
    will = will_for("a4cf12b3de90")
    assert will.topic == "ff/v1/d/a4cf12b3de90/up/presence"
    assert will.payload == PRESENCE_OFFLINE
    assert will.qos == 1
    assert will.retain is True


async def test_the_client_factory_applies_the_will_and_a_persistent_session() -> None:
    """`clean_session=False` + `identifier=device_id`: command durability, R0-be-4.

    Reads paho's private attributes because aiomqtt exposes no getter. They are the
    only way to prove the will actually reached the client rather than being built and
    dropped, which is a bug that is invisible until a board dies.
    """
    device_id = "a4cf12b3de90"
    factory = mqtt_client_factory("mosquitto", 1883, credential_for(device_id), device_id)
    client = factory()
    paho = client._client
    assert paho._client_id == device_id.encode()
    assert paho._clean_session is False
    assert paho._will_topic == b"ff/v1/d/a4cf12b3de90/up/presence"
    assert paho._will_payload == PRESENCE_OFFLINE
    assert paho._will_qos == 1
    assert paho._will_retain is True
    # A *fresh* client per call: entering the same one twice is MqttReentrantError,
    # and the sleepy loop connects once per wake.
    assert factory() is not client


# ---------------------------------------------------------------------------
# Import purity
# ---------------------------------------------------------------------------

FORBIDDEN_IMPORTS = (
    "fleetforge.config",
    "fleetforge.db",
    "fleetforge.api",
    "fleetforge.ingestor",
    "fleetforge.auth",
    "fleetforge.broker",
    "fleetforge.presence",
    "sqlalchemy",
    "fastapi",
    "pydantic",
    "httpx",
)

ALLOWED_FLEETFORGE_IMPORTS = ("fleetforge.identity", "fleetforge.simulator")


def imported_modules(path: Path) -> set[str]:
    """Every module named by an `import` / `from … import` in `path`."""
    tree = ast.parse(path.read_text(encoding="utf-8"), filename=str(path))
    modules: set[str] = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            modules.update(alias.name for alias in node.names)
        elif isinstance(node, ast.ImportFrom) and node.module and node.level == 0:
            modules.add(node.module)
    return modules


@pytest.mark.parametrize("path", sorted(SIMULATOR_DIR.glob("*.py")), ids=lambda p: p.name)
def test_the_simulator_imports_nothing_from_the_server(path: Path) -> None:
    """A simulator sharing the server's parsing agrees with it by construction.

    `fleetforge.identity` is the one deliberate exception: the device-id format is a
    *contract*. `fleetforge.config` is banned specifically because importing it makes
    `DATABASE_URL` mandatory to run a fake board, and `httpx` because it is a dev
    dependency while this package ships in the production image (`storage/__main__.py`).
    """
    for module in imported_modules(path):
        for forbidden in FORBIDDEN_IMPORTS:
            assert module != forbidden and not module.startswith(f"{forbidden}."), (
                f"{path.name} imports {module}"
            )
        if module.startswith("fleetforge"):
            assert module.startswith(ALLOWED_FLEETFORGE_IMPORTS), f"{path.name} imports {module}"


def test_the_purity_tripwire_can_actually_fail(tmp_path: Path) -> None:
    """Non-vacuity: prove the AST walk sees an import it is supposed to catch."""
    sample = tmp_path / "bad.py"
    sample.write_text("import sqlalchemy\nfrom fleetforge.config import get_settings\n")
    assert imported_modules(sample) == {"sqlalchemy", "fleetforge.config"}


# ---------------------------------------------------------------------------
# Secret hygiene
# ---------------------------------------------------------------------------


async def test_the_broker_password_never_reaches_the_transcript_or_the_logs(
    capsys: pytest.CaptureFixture[str],
) -> None:
    """`mqtt_password` lives in the enroll response and a 0600 file. Nowhere else."""
    device = DeviceIdentity(device_id="a4cf12b3de90")
    lines: list[str] = []

    with capture_logs() as records:
        await drive_session(device, step=lines.append)

    assert lines, "the transcript must not be empty, or this test proves nothing"
    assert any(line.startswith("connect ") for line in lines)
    joined = "\n".join(lines)
    assert SECRET not in joined
    assert device.device_id in joined, "the username (= device_id) is not a secret"
    assert not any(SECRET in record.getMessage() for record in records)
    captured = capsys.readouterr()
    assert SECRET not in captured.out
    assert SECRET not in captured.err


def test_describe_names_the_file_without_naming_the_credential(tmp_path: Path) -> None:
    credential = credential_for("a4cf12b3de90")
    path = save(tmp_path, credential)
    rendered = describe(path)
    assert rendered == f"{path} (0600)"
    assert SECRET not in rendered


# ---------------------------------------------------------------------------
# The state store (the NVS analogue) — CRITICAL
# ---------------------------------------------------------------------------


def test_state_is_written_0600_in_a_0700_directory(tmp_path: Path) -> None:
    """A world-readable file here is a leaked live fleet credential."""
    state_dir = tmp_path / "nested" / ".sim"
    path = save(state_dir, credential_for("a4cf12b3de90"))
    assert stat.S_IMODE(path.stat().st_mode) == 0o600
    assert stat.S_IMODE(state_dir.stat().st_mode) == 0o700
    assert path == state_path(state_dir, "a4cf12b3de90")


def test_state_round_trips(tmp_path: Path) -> None:
    credential = credential_for("a4cf12b3de90")
    save(tmp_path, credential)
    assert load(tmp_path, "a4cf12b3de90") == credential
    assert load(tmp_path, "b4cf12b3de90") is None


def test_a_preexisting_world_readable_file_is_repaired(tmp_path: Path) -> None:
    """`O_CREAT`'s mode applies only on creation, so `save` chmods as well."""
    path = state_path(tmp_path, "a4cf12b3de90")
    tmp_path.mkdir(exist_ok=True)
    path.write_text("{}")
    os.chmod(path, 0o644)
    save(tmp_path, credential_for("a4cf12b3de90"))
    assert stat.S_IMODE(path.stat().st_mode) == 0o600


@pytest.mark.parametrize(
    ("body", "match"),
    [
        ("not json at all", "not valid JSON"),
        ("[]", "not a JSON object"),
        ('{"device_id": "a4cf12b3de90"}', "missing"),
        (
            '{"device_id": "ffffffffffff", "mqtt_username": "x", "mqtt_password": "y", '
            '"api_base": "z", "enrolled_at": "t"}',
            "copy-paste",
        ),
    ],
)
def test_unusable_state_raises_instead_of_re_enrolling(
    tmp_path: Path, body: str, match: str
) -> None:
    """`None` means "enroll", and enrolling burns a single-use token. Never guess."""
    state_path(tmp_path, "a4cf12b3de90").write_text(body)
    with pytest.raises(SimulatorError, match=match):
        load(tmp_path, "a4cf12b3de90")


def test_forget_removes_the_credential_once(tmp_path: Path) -> None:
    save(tmp_path, credential_for("a4cf12b3de90"))
    assert forget(tmp_path, "a4cf12b3de90") is True
    assert forget(tmp_path, "a4cf12b3de90") is False
    assert load(tmp_path, "a4cf12b3de90") is None


async def test_a_second_run_reuses_the_credential_and_makes_no_http_call(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The single most expensive bug this simulator could have: a silent re-enroll."""

    async def explode(*_args: object, **_kwargs: object) -> None:
        raise AssertionError("a board with state on disk must not enroll again")

    monkeypatch.setattr(cli, "enroll", explode)
    identity = DeviceIdentity(device_id="a4cf12b3de90")
    save(tmp_path, credential_for(identity.device_id))
    args = cli.build_parser().parse_args(["run", "--name", "x", "--state-dir", str(tmp_path)])
    lines, step = transcript()

    credential = await cli._credential_for(args, identity, None, LinkProfile(LINK_FAST), step)

    assert credential.mqtt_password == SECRET
    assert any("reusing" in line for line in lines)
    assert SECRET not in "\n".join(lines)


async def test_no_state_and_no_token_is_a_loud_refusal(tmp_path: Path) -> None:
    args = cli.build_parser().parse_args(["run", "--name", "x", "--state-dir", str(tmp_path)])
    with pytest.raises(SimulatorError, match="no --token"):
        await cli._credential_for(
            args,
            DeviceIdentity(device_id="a4cf12b3de90"),
            None,
            LinkProfile(LINK_FAST),
            lambda _m: None,
        )


# ---------------------------------------------------------------------------
# The link profile
# ---------------------------------------------------------------------------


def test_a_fast_link_adds_nothing() -> None:
    link = LinkProfile(LINK_FAST)
    assert [link.next_delay() for _ in range(5)] == [0.0] * 5
    assert link.enroll_delay() == 0.0


def test_a_slow_link_is_reproducible_and_bounded() -> None:
    """Same seed, same delays — "it survived a bad link" must be repeatable."""
    first = [LinkProfile(LINK_SLOW, seed=7).next_delay() for _ in range(3)]
    again = [LinkProfile(LINK_SLOW, seed=7).next_delay() for _ in range(3)]
    assert first == again
    other = LinkProfile(LINK_SLOW, seed=8)
    delays = [other.next_delay() for _ in range(20)]
    assert all(SLOW_MIN_DELAY_S <= delay <= SLOW_MAX_DELAY_S for delay in delays)
    assert len(set(delays)) > 1, "a 'slow' link that always waits the same is not jitter"
    assert other.enroll_delay() > SLOW_MIN_DELAY_S


def test_an_unknown_link_profile_is_refused() -> None:
    with pytest.raises(SimulatorError, match="--link must be one of"):
        LinkProfile("lossy")


# ---------------------------------------------------------------------------
# The CLI surface
# ---------------------------------------------------------------------------


def test_the_cli_defaults_are_the_spec_numbers() -> None:
    args = cli.build_parser().parse_args(["run"])
    assert args.heartbeat_interval == 60.0
    assert args.power_class == "always_on"
    assert args.state_dir.endswith(".sim")
    assert args.token is None
    assert args.agent_version.endswith("-sim"), "a fake board must be identifiable"


def test_fleet_requires_a_password_only_when_a_board_must_enroll(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    """And says which credential it wants — the password, not the hash."""
    monkeypatch.delenv("FF_ADMIN_PASSWORD", raising=False)
    code = cli.main(["fleet", "--count", "2", "--state-dir", str(tmp_path)])
    assert code == 1
    assert "ADMIN_PASSWORD_HASH" in capsys.readouterr().err


def test_flatten_unwraps_task_group_failures() -> None:
    """`fleet` failures arrive as an ExceptionGroup; the operator needs the leaves."""
    group = ExceptionGroup(
        "boards", [SimulatorError("a"), ExceptionGroup("inner", [SimulatorError("b")])]
    )
    assert [str(exc) for exc in cli.flatten(group)] == ["a", "b"]
    assert [str(exc) for exc in cli.flatten(SimulatorError("solo"))] == ["solo"]


async def test_reset_reason_names_the_first_boot_and_the_apply_reboot_is_sw(
    downloads: list[str], monkeypatch: pytest.MonkeyPatch
) -> None:
    """R4-be-1: `--reset-reason panic` is this run's first boot; an OTA reboot is still `sw`."""
    monkeypatch.setattr("fleetforge.simulator.device.REBOOT_DELAY_S", 0.0)
    args = cli.build_parser().parse_args(["run", "--reset-reason", "panic"])
    identity = cli._identity_for(args, "a4cf12b3de90")
    assert identity.reset_reason == "panic"
    assert cli.build_parser().parse_args(["run"]).reset_reason == "poweron"
    for bad in ("", "Panic", "x" * 17, "a-b"):
        with pytest.raises(SystemExit):
            cli.build_parser().parse_args(["run", "--reset-reason", bad])
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

    assert channel_payloads(clients[0], identity.device_id, ANNOUNCE)[0]["reset_reason"] == "panic"
    assert channel_payloads(clients[1], identity.device_id, ANNOUNCE)[0]["reset_reason"] == "sw"


# ---------------------------------------------------------------------------
# R5-be-1: a simulated custom self-test. Registered = the `selftest` capability. The
# details it composes must be ones the server's parser reads (the simulator may not
# import the server; this test file may).
# ---------------------------------------------------------------------------

SELFTEST_CAPS = (CAPABILITY_SELFTEST,)


def parsed_details(*fakes: FakeClient) -> list[SelfTestOutcome | None]:
    return [parse_selftest_detail(r.get("detail")) for fake in fakes for r in statuses(fake)]


async def test_a_registered_selftest_that_passes_reports_it_on_the_confirmed(
    downloads: list[str],
) -> None:
    stage = await applied_stage(capabilities=SELFTEST_CAPS, selftest_fail_marker="-stfail")
    on_new = FakeClient()
    await one_session(stage, on_new)

    reports = statuses(on_new)
    assert [r["state"] for r in reports] == list(CONFIRM_WALK)
    assert reports[-1]["detail"] == "selftest passed" == SELFTEST_PASSED
    assert reports[-1]["pct"] == 100
    assert parse_selftest_detail(reports[-1]["detail"]) == SelfTestOutcome("passed", None, None)


async def test_a_board_without_a_selftest_confirms_with_no_detail(downloads: list[str]) -> None:
    stage = await applied_stage()
    on_new = FakeClient()
    await one_session(stage, on_new)

    assert statuses(on_new)[-1].get("detail") is None


async def test_a_failing_selftest_rolls_back_at_once_and_names_the_slot(
    downloads: list[str],
) -> None:
    stage = await applied_stage(
        version="1.5.0-stfail", capabilities=SELFTEST_CAPS, selftest_fail_marker="-stfail"
    )
    on_new = FakeClient()
    await one_session(stage, on_new, awake_s=None)  # ends by the rollback's reboot

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
    stage = await applied_stage(
        version="1.5.0-hang",
        capabilities=SELFTEST_CAPS,
        broken_marker="-hang",
        selftest_fail_marker="-hang",  # broken (hangs) beats fail-marked
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
    await one_session(stage, FakeClient(), awake_s=None)  # the new image fails its self-test
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


# ---------------------------------------------------------------------------
# The R5 verbs (R5-be-2): apply, cancel, rollback and reboot, each answered once on
# `up/ack`, mirroring `ff_mqtt.c` as *R5-fw-4/5 as built* describe it.
# ---------------------------------------------------------------------------

R5_CAPABILITIES = ("ota", "apply", "cancel", "rollback", "reboot")
SIM_ID = "a4cf12b3de90"


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
    # The answer leaves before the walk it causes.
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
    # The next stage is NOT refused: nothing is staged any more.
    assert [s["state"] for s in for_cmd(fake, "cmd-stage-2")][-1] == STATE_STAGED
    assert stage.staged == "cmd-stage-2"
    assert not stage.reboot.is_set()


async def test_cancel_of_nothing_is_refused(downloads: list[str]) -> None:
    fake, _ = await session_with(r5_board(), verb("cancel", "x2", "cmd-stage-1"))
    assert acks(fake)[0]["detail"] == "nothing to cancel for this cmd_id"
    assert statuses(fake) == []


async def confirmed_r5_board(downloads: list[str], **kwargs: Any) -> StageRunner:
    """A board that applied and confirmed 1.5.0 in ota_1, coming from 1.4.2 in ota_0."""
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
    # Nothing to return to any more (ota_1 is marked invalid).
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
    stage = r5_board()
    fake, _ = await session_with(stage, staged_msg(), verb("rollback", "x4"), verb("reboot", "x6"))

    assert [(a["type"], a["result"], a["cmd_id"], a["detail"]) for a in acks(fake)] == [
        ("rollback", "refused", "cmd-stage-1", "cancel first"),
        ("reboot", "refused", "cmd-stage-1", "an update is in progress"),
    ]
    assert not stage.reboot.is_set()


async def test_rollback_while_confirming_ends_the_stages_transaction(downloads: list[str]) -> None:
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
    stage.safe_window = SAFE_WINDOW_HOLD  # the window closes after the confirm

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

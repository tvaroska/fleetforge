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
import json
import os
import stat
from collections.abc import AsyncIterator
from pathlib import Path
from typing import Any

import pytest

from fleetforge.api.schemas import EnrollRequest
from fleetforge.db.models import PowerClass
from fleetforge.identity import DEVICE_ID_RE
from fleetforge.ingestor.protocol import AnnouncePayload, PresencePayload, parse_up_topic
from fleetforge.simulator import __main__ as cli
from fleetforge.simulator.device import (
    ANNOUNCE,
    HEARTBEAT,
    LINK_FAST,
    LINK_SLOW,
    POWER_CLASSES,
    PRESENCE,
    PRESENCE_OFFLINE,
    PRESENCE_ONLINE,
    QOS,
    SLOW_MAX_DELAY_S,
    SLOW_MIN_DELAY_S,
    DeviceIdentity,
    LinkProfile,
    derive_device_id,
    dn_filter,
    mqtt_client_factory,
    run_session,
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
    "power_class",
    "expected_wake_interval_s",
    "parent_device_id",
    "partition_layout",
    "ota_slot_size",
    "capabilities",
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


@pytest.mark.parametrize(
    "device",
    [
        DeviceIdentity(device_id="a4cf12b3de90"),
        DeviceIdentity(
            device_id="a4cf12b3de91", power_class="sleepy", expected_wake_interval_s=300
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


def test_heartbeat_body_is_the_spec_body() -> None:
    body = DeviceIdentity(device_id="a4cf12b3de90").heartbeat(41)
    assert set(body) == {"fw_version", "uptime_s", "rssi", "free_heap", "boot_ok"}
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
    assert len(commands) == 2
    assert "DUPLICATE" in commands[1]
    assert "not executed" in commands[0]


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

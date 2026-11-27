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

from __future__ import annotations

import ast
import asyncio
import json
import os
import stat
from pathlib import Path

import pytest

from fleetforge.api.schemas import EnrollRequest
from fleetforge.db.models import PowerClass
from fleetforge.identity import DEVICE_ID_RE
from fleetforge.ingestor.protocol import (
    AnnouncePayload,
    PresencePayload,
    parse_up_topic,
)
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
    run_always_on,
    run_session,
    up_topic,
    will_for,
)
from fleetforge.simulator.errors import SimulatorError
from fleetforge.simulator.state import describe, forget, load, save, state_path
from tests.conftest import capture_logs
from tests.doubles import (
    FIRMWARE as FIRMWARE,
)
from tests.doubles import (
    FIRMWARE_SHA256 as FIRMWARE_SHA256,
)
from tests.doubles import (
    SECRET as SECRET,
)
from tests.doubles import (
    SIGNED_URL as SIGNED_URL,
)
from tests.doubles import (
    FakeClient as FakeClient,
)
from tests.doubles import (
    FakeHttpResponse as FakeHttpResponse,
)
from tests.doubles import (
    FakeMessage as FakeMessage,
)
from tests.doubles import (
    channel_payloads as channel_payloads,
)
from tests.doubles import (
    credential_for as credential_for,
)
from tests.doubles import (
    drive_session as drive_session,
)
from tests.doubles import (
    drive_until as drive_until,
)
from tests.doubles import (
    session_factory as session_factory,
)
from tests.doubles import (
    stage_command as stage_command,
)
from tests.doubles import (
    statuses as statuses,
)
from tests.doubles import (
    transcript as transcript,
)

SIMULATOR_DIR = Path(__file__).resolve().parent.parent / "src" / "fleetforge" / "simulator"


@pytest.fixture
def downloads(monkeypatch: pytest.MonkeyPatch) -> list[str]:
    """Record every URL fetched and serve `FIRMWARE`. One entry per real download."""
    fetched: list[str] = []

    def fake_urlopen(url: str, timeout: float | None = None) -> FakeHttpResponse:
        fetched.append(url)
        return FakeHttpResponse(FIRMWARE)

    monkeypatch.setattr("urllib.request.urlopen", fake_urlopen)
    return fetched


@pytest.mark.parametrize("name", ["blinker", "sim", "sim-01", "", "Ünïcødé", "a" * 200])
def test_derived_ids_are_valid_locally_administered_unicast_macs(name: str) -> None:
    """The id must satisfy `DEVICE_ID_RE` and be provably not a real eFuse MAC."""
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
    """A nested `{"token": ..., "identity": {...}}` body would be a recall (schemas.py)."""
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


@pytest.mark.parametrize("channel", [ANNOUNCE, PRESENCE, HEARTBEAT])
def test_up_topics_round_trip_through_the_ingestors_parser(channel: str) -> None:
    parsed = parse_up_topic(up_topic("a4cf12b3de90", channel))
    assert parsed is not None
    assert parsed.device_id == "a4cf12b3de90"
    assert parsed.channel == channel


def test_dn_filter_is_the_pattern_acl_the_broker_grants() -> None:
    """`mosquitto/acl` grants `ff/v1/d/%u/dn/#` and nothing wider."""
    assert dn_filter("a4cf12b3de90") == "ff/v1/d/a4cf12b3de90/dn/#"
    assert parse_up_topic("ff/v1/d/a4cf12b3de90/dn/cmd") is None


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
    assert any("not implemented" in line for line in commands)


async def test_an_undecodable_command_does_not_kill_the_session() -> None:
    device = DeviceIdentity(device_id="a4cf12b3de90")
    fake = FakeClient()
    fake.inbox.put_nowait(FakeMessage(f"ff/v1/d/{device.device_id}/dn/cmd", b"\xff not json"))
    lines, step = transcript()

    await drive_session(device, awake_s=0.15, step=step, client=fake)

    assert any("undecodable" in line for line in lines)
    assert any(line.startswith("goodbye") for line in lines)


def test_the_will_is_a_retained_offline_presence() -> None:
    """Retained on purpose: a server that restarts must still learn the board is gone."""
    will = will_for("a4cf12b3de90")
    assert will.topic == "ff/v1/d/a4cf12b3de90/up/presence"
    assert will.payload == PRESENCE_OFFLINE
    assert will.qos == 1
    assert will.retain is True


async def test_the_client_factory_applies_the_will_and_a_persistent_session() -> None:
    """`clean_session=False` + `identifier=device_id`: command durability, R0-be-4."""
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
    assert factory() is not client


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
    """Every module named by an `import` / `from ... import` in `path`."""
    tree = ast.parse(path.read_text(encoding="utf-8"), filename=str(path))
    modules: set[str] = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            modules.update(alias.name for alias in node.names)
        elif isinstance(node, ast.ImportFrom) and node.module and node.level == 0:
            modules.add(node.module)
    return modules


def _simulator_py_files(root: Path = SIMULATOR_DIR) -> list[Path]:
    return sorted(root.rglob("*.py"))


@pytest.mark.parametrize(
    "path",
    _simulator_py_files(),
    ids=lambda p: p.relative_to(SIMULATOR_DIR).as_posix(),
)
def test_the_simulator_imports_nothing_from_the_server(path: Path) -> None:
    """A simulator sharing the server's parsing agrees with it by construction."""
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


def test_the_purity_guard_sees_subpackages(tmp_path: Path) -> None:
    """T-9: `rglob` discovers nested simulator subpackages rather than stopping at flat `*.py`."""
    nested = tmp_path / "subpkg" / "leak.py"
    nested.parent.mkdir(parents=True)
    nested.write_text("import fastapi\n", encoding="utf-8")
    discovered = _simulator_py_files(tmp_path)
    assert discovered == [nested]
    assert "fastapi" in imported_modules(discovered[0])


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
    _ = downloads
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

"""Parameterized `@pytest.mark.port` conformance suites for broker Ports (`T-8` / `T-30`).

Holds `FakeCommandPublisher` and `FakeDynsecSession` to the exact same contract as the real
`MqttCommandPublisher` and `MqttDynsecSession` adapters so fakes can never drift silently.
"""

from __future__ import annotations

import ast
import json
from collections.abc import AsyncIterator
from dataclasses import dataclass
from pathlib import Path
from typing import Any

import aiomqtt
import pytest

from fleetforge.broker import (
    DEVICE_ROLE,
    BrokerProvisioningError,
    CommandPublisher,
    CommandPublishError,
    NullCommandPublisher,
    cfg_payload,
    cfg_topic,
    command_topic,
    stage_payload,
)
from fleetforge.broker.dynsec import (
    CREATE_CLIENT,
    DELETE_CLIENT,
    DYNSEC_CONTROL_TOPIC,
    ENABLE_CLIENT,
    SET_CLIENT_PASSWORD,
    DynsecProvisioner,
    DynsecSession,
    MqttDynsecSession,
    retained_up_topics,
)
from fleetforge.broker.publisher import CFG_RETAIN, QOS, RETAIN, MqttCommandPublisher
from tests.conftest import FakeCommandPublisher
from tests.doubles import FakeDynsecSession, FakeMqttClient

pytestmark = pytest.mark.port

DEVICE_A = "a4cf12b3de01"
DEVICE_B = "a4cf12b3de02"


def _sample_stage(cmd_id: str = "c01") -> dict[str, Any]:
    return stage_payload(
        cmd_id=cmd_id,
        url="https://downloads.test/v1/artifact/" + "a" * 64 + "/bin?exp=1&sig=x",
        sha256="a" * 64,
        size=1024,
        version="1.5.0",
        apply="auto",
        confirm_timeout_s=300,
    )


@dataclass
class _PublisherHarness:
    publisher: CommandPublisher
    set_failure: Any
    sent_cmds: Any
    sent_cfgs: Any
    cleared_cfg_devices: Any


@pytest.fixture(params=["fake", "mqtt"])
def publisher_harness(request: pytest.FixtureRequest) -> _PublisherHarness:
    kind = str(request.param)
    if kind == "fake":
        fake = FakeCommandPublisher()
        return _PublisherHarness(
            publisher=fake,
            set_failure=lambda exc: setattr(fake, "fail_with", exc),
            sent_cmds=lambda: list(fake.published),
            sent_cfgs=lambda: list(fake.published_cfg),
            cleared_cfg_devices=lambda: list(fake.cleared_cfg),
        )
    if kind == "mqtt":
        client = FakeMqttClient()
        mqtt_pub = MqttCommandPublisher(client, timeout_s=5.0)  # type: ignore[arg-type]

        def _set_failure(exc: Exception | None) -> None:
            client.fail_with = (
                aiomqtt.MqttError(str(exc)) if isinstance(exc, CommandPublishError) else exc
            )

        return _PublisherHarness(
            publisher=mqtt_pub,
            set_failure=_set_failure,
            sent_cmds=lambda: [
                (t.split("/")[3], json.loads(raw))
                for t, raw, qos, retain in client.published
                if t.endswith("/dn/cmd") and qos == QOS and retain is RETAIN and raw
            ],
            sent_cfgs=lambda: [
                (t.split("/")[3], json.loads(raw))
                for t, raw, qos, retain in client.published
                if t.endswith("/dn/cfg") and qos == QOS and retain is CFG_RETAIN and raw
            ],
            cleared_cfg_devices=lambda: [
                t.split("/")[3]
                for t, raw, qos, retain in client.published
                if t.endswith("/dn/cfg") and qos == QOS and retain is CFG_RETAIN and raw == b""
            ],
        )
    raise AssertionError(f"unexpected publisher kind {kind!r}")


async def test_command_publisher_single_and_batch_verbs(
    publisher_harness: _PublisherHarness,
) -> None:
    pub = publisher_harness.publisher
    c1, c2, c3 = _sample_stage("c1"), _sample_stage("c2"), _sample_stage("c3")
    cfg1 = cfg_payload(secrets_rev=2, stay_awake=True)
    cfg2 = cfg_payload(secrets_rev=3)
    cfg3 = cfg_payload(secrets_rev=4)

    await pub.publish(DEVICE_A, c1)
    await pub.publish_cfg(DEVICE_A, cfg1)
    await pub.clear_cfg(DEVICE_A)
    await pub.publish_many([(DEVICE_A, c2), (DEVICE_B, c3)])
    await pub.publish_cfg_many([(DEVICE_A, cfg2), (DEVICE_B, cfg3)])

    assert publisher_harness.sent_cmds() == [(DEVICE_A, c1), (DEVICE_A, c2), (DEVICE_B, c3)]
    assert publisher_harness.sent_cfgs() == [(DEVICE_A, cfg1), (DEVICE_A, cfg2), (DEVICE_B, cfg3)]
    assert publisher_harness.cleared_cfg_devices() == [DEVICE_A]


async def test_command_publisher_empty_batches_and_serialization_contract(
    publisher_harness: _PublisherHarness,
) -> None:
    pub = publisher_harness.publisher
    with pytest.raises(TypeError):
        await pub.publish(DEVICE_A, {"bad": object()})
    with pytest.raises(TypeError):
        await pub.publish_many((DEVICE_A, _sample_stage("gen")) for _ in range(1))  # type: ignore[arg-type]

    publisher_harness.set_failure(CommandPublishError("broker offline"))
    await pub.publish_many([])
    await pub.publish_cfg_many([])
    assert publisher_harness.sent_cmds() == []
    assert publisher_harness.sent_cfgs() == []


async def test_command_publisher_failure_raises_command_publish_error_across_all_verbs(
    publisher_harness: _PublisherHarness,
) -> None:
    pub = publisher_harness.publisher
    publisher_harness.set_failure(CommandPublishError("broker offline"))

    with pytest.raises(CommandPublishError):
        await pub.publish(DEVICE_A, _sample_stage("c1"))
    with pytest.raises(CommandPublishError):
        await pub.publish_cfg(DEVICE_A, cfg_payload(secrets_rev=1))
    with pytest.raises(CommandPublishError):
        await pub.clear_cfg(DEVICE_A)
    with pytest.raises(CommandPublishError):
        await pub.publish_many([(DEVICE_A, _sample_stage("c2"))])
    with pytest.raises(CommandPublishError):
        await pub.publish_cfg_many([(DEVICE_A, cfg_payload(secrets_rev=2))])


async def test_null_command_publisher_conforms_to_port_error_and_empty_batch_contract() -> None:
    pub: CommandPublisher = NullCommandPublisher()
    await pub.publish_many([])
    await pub.publish_cfg_many([])

    with pytest.raises(CommandPublishError):
        await pub.publish(DEVICE_A, _sample_stage("c1"))
    with pytest.raises(CommandPublishError):
        await pub.publish_cfg(DEVICE_A, cfg_payload(secrets_rev=1))
    with pytest.raises(CommandPublishError):
        await pub.clear_cfg(DEVICE_A)
    with pytest.raises(CommandPublishError):
        await pub.publish_many([(DEVICE_A, _sample_stage("c2"))])
    with pytest.raises(CommandPublishError):
        await pub.publish_cfg_many([(DEVICE_A, cfg_payload(secrets_rev=2))])
    assert command_topic(DEVICE_A).endswith("/dn/cmd")
    assert cfg_topic(DEVICE_A).endswith("/dn/cfg")


class _FakeRawMessage:
    def __init__(self, payload: bytes) -> None:
        self.payload = payload


class _ScriptedMqttDynsecControl:
    """In-memory `aiomqtt.Client` stand-in driving the real `MqttDynsecSession`."""

    def __init__(self, errors: list[str | None]) -> None:
        self._errors = list(errors)
        self.commands: list[dict[str, Any]] = []
        self._replies: list[_FakeRawMessage] = []

    async def publish(self, topic: str, payload: bytes, *, qos: int) -> None:
        assert topic == DYNSEC_CONTROL_TOPIC
        assert qos == 1
        cmd = json.loads(payload)["commands"][0]
        self.commands.append(cmd)
        err = self._errors.pop(0)
        entry: dict[str, Any] = {
            "command": cmd["command"],
            "correlationData": cmd["correlationData"],
        }
        if err is not None:
            entry["error"] = err
        self._replies.append(_FakeRawMessage(json.dumps({"responses": [entry]}).encode("utf-8")))

    async def messages_iter(self) -> AsyncIterator[_FakeRawMessage]:
        while self._replies:
            yield self._replies.pop(0)


@dataclass
class _DynsecHarness:
    session_factory: Any
    commands: list[dict[str, Any]]
    cleared_calls: list[tuple[str, str]]
    cleared_topics: list[tuple[str, bytes, int, bool]]


def _make_dynsec_harness(
    kind: str,
    monkeypatch: pytest.MonkeyPatch,
    command_errors: list[tuple[str, str | None]],
    *,
    clear_fail_with: Exception | None = None,
) -> _DynsecHarness:
    if kind == "fake":
        replies: list[bytes | Exception] = []
        for cmd_name, err in command_errors:
            entry: dict[str, Any] = {"command": cmd_name, "correlationData": "placeholder"}
            if err is not None:
                entry["error"] = err
            replies.append(json.dumps({"responses": [entry]}).encode("utf-8"))
        fake = FakeDynsecSession(replies, clear_fail_with=clear_fail_with)
        return _DynsecHarness(
            session_factory=fake,
            commands=fake.commands,
            cleared_calls=fake.cleared_retained,
            cleared_topics=[],
        )

    if kind == "mqtt":
        control = _ScriptedMqttDynsecControl([err for _, err in command_errors])
        cleared_calls: list[tuple[str, str]] = []
        cleared_topics: list[tuple[str, bytes, int, bool]] = []

        class _FakeDeviceClient:
            def __init__(self, **kwargs: Any) -> None:
                self._username = str(kwargs["username"])
                self._password = str(kwargs["password"])

            async def __aenter__(self) -> _FakeDeviceClient:
                if clear_fail_with is not None:
                    raise clear_fail_with
                cleared_calls.append((self._username, self._password))
                return self

            async def __aexit__(self, *exc_info: object) -> None:
                return None

            async def publish(self, topic: str, payload: bytes, *, qos: int, retain: bool) -> None:
                cleared_topics.append((topic, payload, qos, retain))

        monkeypatch.setattr(aiomqtt, "Client", _FakeDeviceClient)
        session = MqttDynsecSession(
            control,  # type: ignore[arg-type]
            control.messages_iter(),  # type: ignore[arg-type]
            hostname="mosquitto.test",
            port=1883,
        )

        class _SessionCtx:
            def __call__(self) -> _SessionCtx:
                return self

            async def __aenter__(self) -> DynsecSession:
                return session

            async def __aexit__(self, *exc_info: object) -> None:
                return None

        return _DynsecHarness(
            session_factory=_SessionCtx(),
            commands=control.commands,
            cleared_calls=cleared_calls,
            cleared_topics=cleared_topics,
        )

    raise AssertionError(f"unexpected dynsec kind {kind!r}")


@pytest.mark.parametrize("kind", ["fake", "mqtt"])
async def test_dynsec_ensure_client_create_and_already_exists_parity(
    kind: str, monkeypatch: pytest.MonkeyPatch
) -> None:
    created = _make_dynsec_harness(kind, monkeypatch, [(CREATE_CLIENT, None)])
    assert (
        await DynsecProvisioner(
            created.session_factory, role=DEVICE_ROLE, timeout_s=5.0
        ).ensure_client(DEVICE_A, "pw-1")
        is True
    )
    assert [c["command"] for c in created.commands] == [CREATE_CLIENT]

    existing = _make_dynsec_harness(
        kind,
        monkeypatch,
        [
            (CREATE_CLIENT, "Client already exists"),
            (SET_CLIENT_PASSWORD, None),
            (ENABLE_CLIENT, None),
        ],
    )
    assert (
        await DynsecProvisioner(
            existing.session_factory, role=DEVICE_ROLE, timeout_s=5.0
        ).ensure_client(DEVICE_A, "pw-2")
        is True
    )
    assert [c["command"] for c in existing.commands] == [
        CREATE_CLIENT,
        SET_CLIENT_PASSWORD,
        ENABLE_CLIENT,
    ]


@pytest.mark.parametrize("kind", ["fake", "mqtt"])
async def test_dynsec_remove_client_rotates_clears_and_deletes_identically(
    kind: str, monkeypatch: pytest.MonkeyPatch
) -> None:
    harness = _make_dynsec_harness(
        kind,
        monkeypatch,
        [(SET_CLIENT_PASSWORD, None), (ENABLE_CLIENT, None), (DELETE_CLIENT, None)],
    )
    provisioner = DynsecProvisioner(harness.session_factory, role=DEVICE_ROLE, timeout_s=5.0)

    assert await provisioner.remove_client(DEVICE_A) is True
    assert [c["command"] for c in harness.commands] == [
        SET_CLIENT_PASSWORD,
        ENABLE_CLIENT,
        DELETE_CLIENT,
    ]
    rotated_pw = harness.commands[0]["password"]
    assert harness.cleared_calls == [(DEVICE_A, rotated_pw)]
    if kind == "mqtt":
        assert harness.cleared_topics == [
            (topic, b"", 1, True) for topic in retained_up_topics(DEVICE_A)
        ]


@pytest.mark.parametrize("kind", ["fake", "mqtt"])
async def test_dynsec_remove_client_skips_clear_when_client_not_found(
    kind: str, monkeypatch: pytest.MonkeyPatch
) -> None:
    harness = _make_dynsec_harness(
        kind,
        monkeypatch,
        [(SET_CLIENT_PASSWORD, "Client not found"), (DELETE_CLIENT, "Client not found")],
    )
    provisioner = DynsecProvisioner(harness.session_factory, role=DEVICE_ROLE, timeout_s=5.0)

    assert await provisioner.remove_client(DEVICE_A) is True
    assert [c["command"] for c in harness.commands] == [SET_CLIENT_PASSWORD, DELETE_CLIENT]
    assert harness.cleared_calls == []


@pytest.mark.parametrize("kind", ["fake", "mqtt"])
async def test_dynsec_clear_retained_transport_error_becomes_provisioning_error(
    kind: str, monkeypatch: pytest.MonkeyPatch
) -> None:
    harness = _make_dynsec_harness(
        kind,
        monkeypatch,
        [(SET_CLIENT_PASSWORD, None), (ENABLE_CLIENT, None)],
        clear_fail_with=aiomqtt.MqttError("broker disconnect"),
    )
    provisioner = DynsecProvisioner(harness.session_factory, role=DEVICE_ROLE, timeout_s=5.0)
    with pytest.raises(BrokerProvisioningError):
        await provisioner.remove_client(DEVICE_A)


def test_dynsec_provisioner_has_no_isinstance_branch_on_mqtt_session() -> None:
    """T-30 / T-8: `remove_client` must speak the `DynsecSession` Port, never branch on concrete type."""
    source = (
        Path(__file__).resolve().parents[2] / "src" / "fleetforge" / "broker" / "dynsec.py"
    ).read_text(encoding="utf-8")
    tree = ast.parse(source)
    for node in ast.walk(tree):
        if (
            isinstance(node, ast.Call)
            and isinstance(node.func, ast.Name)
            and node.func.id == "isinstance"
            and len(node.args) >= 2
            and any(
                isinstance(child, ast.Name) and child.id == "MqttDynsecSession"
                for child in ast.walk(node.args[1])
            )
        ):
            pytest.fail(
                f"dynsec.py branches on MqttDynsecSession via isinstance at line {node.lineno}"
            )

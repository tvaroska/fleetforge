"""The `dn/cmd` seam, with **no broker and no database**. R1-be-2.

Same split as `test_broker_dynsec.py`: everything provable without a socket is proved
here, and the live ACL matrix (may the `commander` credential actually publish? does the
addressed board actually receive?) belongs to `just broker-check`, which is deliberately
not part of `just test`.

Three properties here are the contract rather than plumbing:

* the `stage` body is **exactly** the spec's keys — an extra one reaches a flash-baked
  agent that cannot be updated to ignore it;
* `retain` is `False` and `qos` is `1` on the real publisher, because a retained
  `dn/cmd` re-stages the same firmware on every reconnect, forever;
* `NullCommandPublisher` **raises**, unlike `NullProvisioner`, which succeeds silently —
  a 202 for a command nobody published would write a `requested` row into the KPI
  history for a deploy no board will ever see.
"""

import json
import logging
from typing import Any

import aiomqtt
import pytest

from fleetforge.broker import (
    CommandPublishError,
    NullCommandPublisher,
    command_topic,
    new_command_id,
    stage_payload,
)
from fleetforge.broker.commands import (
    APPLY_AUTO,
    APPLY_MODES,
    APPLY_ON_COMMAND,
    COMMAND_STAGE,
    encode_command,
)
from fleetforge.broker.publisher import QOS, RETAIN, MqttCommandPublisher, mqtt_command_client
from tests.conftest import capture_logs, settings_for_tests

DEVICE_ID = "a4cf12b3de90"
CMD_ID = "0f5b6c7d8e9f40112233445566778899"
SHA256 = "a" * 64
# A signed URL is a bearer credential: several tests assert it never leaves the payload.
URL = f"https://storage.invalid/blobs/sha256/{SHA256}?X-Goog-Signature=deadbeef"


def _payload(**overrides: Any) -> dict[str, Any]:
    values: dict[str, Any] = {
        "cmd_id": CMD_ID,
        "url": URL,
        "sha256": SHA256,
        "size": 1024,
        "version": "1.5.0",
        "apply": APPLY_AUTO,
        "confirm_timeout_s": 300,
    }
    values.update(overrides)
    return stage_payload(**values)


# ---------------------------------------------------------------------------
# Pure: the command on the wire
# ---------------------------------------------------------------------------


def test_stage_payload_is_exactly_the_spec_keys() -> None:
    """`spec/device-protocol.md` → *`dn/cmd` — commands*, key for key."""
    assert _payload() == {
        "id": CMD_ID,
        "type": COMMAND_STAGE,
        "artifact": {"url": URL, "sha256": SHA256, "size": 1024, "version": "1.5.0"},
        "apply": APPLY_AUTO,
        "confirm_timeout_s": 300,
    }


def test_stage_payload_carries_no_signature_field() -> None:
    """R1 has no artifact signer; an empty `sig` would teach a device to accept one."""
    assert "sig" not in _payload()["artifact"]


def test_stage_payload_takes_the_confirm_timeout_from_the_caller() -> None:
    """One number, one place: `Settings.confirm_timeout_s`, never a literal in a builder."""
    assert _payload(confirm_timeout_s=42)["confirm_timeout_s"] == 42


@pytest.mark.parametrize("apply", APPLY_MODES)
def test_both_apply_modes_survive_the_round_trip(apply: str) -> None:
    assert json.loads(encode_command(_payload(apply=apply)))["apply"] == apply


def test_apply_modes_are_the_two_the_spec_names() -> None:
    assert APPLY_MODES == (APPLY_AUTO, APPLY_ON_COMMAND) == ("auto", "on_command")


def test_command_topic_is_the_devices_own_downlink() -> None:
    assert command_topic(DEVICE_ID) == f"ff/v1/d/{DEVICE_ID}/dn/cmd"


def test_command_topic_does_not_normalise_the_device_id() -> None:
    """The id is also the MQTT username the `%u` pattern ACL binds to."""
    assert command_topic("AABBCC112233") == "ff/v1/d/AABBCC112233/dn/cmd"


def test_encode_command_is_compact_utf8_json() -> None:
    raw = encode_command(_payload())
    assert b", " not in raw and b'": ' not in raw
    assert json.loads(raw) == _payload()


def test_new_command_id_is_unique_and_hex() -> None:
    ids = {new_command_id() for _ in range(50)}
    assert len(ids) == 50
    assert all(len(value) == 32 and int(value, 16) >= 0 for value in ids)


# ---------------------------------------------------------------------------
# The real publisher, driven through an injected fake client
# ---------------------------------------------------------------------------


class FakeMqttClient:
    """Records publishes, or raises the scripted failure at connect."""

    def __init__(self, fail_with: Exception | None = None) -> None:
        self.fail_with = fail_with
        # (topic, payload, qos, retain) per publish, in order.
        self.published: list[tuple[str, bytes, int, bool]] = []
        self.entered = 0

    def __call__(self) -> "FakeMqttClient":
        return self

    async def __aenter__(self) -> "FakeMqttClient":
        self.entered += 1
        if self.fail_with is not None:
            raise self.fail_with
        return self

    async def __aexit__(self, *exc_info: object) -> None:
        return None

    async def publish(self, topic: str, payload: bytes, *, qos: int, retain: bool) -> None:
        self.published.append((topic, payload, qos, retain))


def _publisher(client: FakeMqttClient, *, timeout_s: float = 5.0) -> MqttCommandPublisher:
    return MqttCommandPublisher(client, timeout_s=timeout_s)  # type: ignore[arg-type]


async def test_publish_goes_to_the_devices_topic_at_qos_1_and_never_retained() -> None:
    client = FakeMqttClient()
    payload = _payload()

    await _publisher(client).publish(DEVICE_ID, payload)

    (topic, raw, qos, retain) = client.published[0]
    assert topic == command_topic(DEVICE_ID)
    assert json.loads(raw) == payload
    assert qos == 1
    # A retained command re-stages on every reconnect, forever.
    assert retain is False
    assert (QOS, RETAIN) == (1, False), "the module constants are the contract"


async def test_each_publish_opens_its_own_connection() -> None:
    """One short-lived client per command — not a singleton anything can leak."""
    client = FakeMqttClient()
    publisher = _publisher(client)

    await publisher.publish(DEVICE_ID, _payload())
    await publisher.publish(DEVICE_ID, _payload())

    assert client.entered == 2


@pytest.mark.parametrize(
    "failure", [aiomqtt.MqttError("broker gone"), OSError("connection refused")]
)
async def test_transport_failures_become_command_publish_errors(failure: Exception) -> None:
    """No aiomqtt exception may escape into a handler: that is a 500 where a 503 belongs."""
    with pytest.raises(CommandPublishError):
        await _publisher(FakeMqttClient(fail_with=failure)).publish(DEVICE_ID, _payload())


async def test_a_hung_broker_becomes_a_command_publish_error() -> None:
    class HangingClient(FakeMqttClient):
        async def __aenter__(self) -> "FakeMqttClient":
            import asyncio

            await asyncio.sleep(10)
            return self

    with pytest.raises(CommandPublishError):
        await _publisher(HangingClient(), timeout_s=0.05).publish(DEVICE_ID, _payload())


async def test_the_publish_log_line_names_the_command_but_never_the_url() -> None:
    """The payload is a credential; only its `id` and `type` may be logged."""
    with capture_logs() as records:
        await _publisher(FakeMqttClient()).publish(DEVICE_ID, _payload())

    messages = [record.getMessage() for record in records]
    assert any(CMD_ID in message for message in messages)
    assert not any(URL in message or "X-Goog-Signature" in message for message in messages)


async def test_the_client_uses_the_commander_credential_and_a_unique_identifier() -> None:
    """Never the dynsec admin: that is broker-root over `$CONTROL`, a separate privilege.

    `async` only because `aiomqtt.Client.__init__` grabs the running loop.
    """
    settings = settings_for_tests(
        mqtt_command_username="ff-commander",
        mqtt_command_password="commander-dev-only",
        mqtt_dynsec_username="broker-root",
        mqtt_dynsec_password="dynsec-dev-only",
    )

    first = mqtt_command_client(settings)
    second = mqtt_command_client(settings)

    assert first._client._username == b"ff-commander"  # noqa: SLF001 - paho has no accessor
    # Two workers sharing an MQTT client id kick each other off mid-command.
    assert first.identifier != second.identifier
    assert first.identifier.startswith("fleetforge-cmd-")


# ---------------------------------------------------------------------------
# The Null adapter
# ---------------------------------------------------------------------------


async def test_null_publisher_raises_rather_than_pretending() -> None:
    """Unlike `NullProvisioner`: a silent success here becomes a lying `requested` row."""
    with capture_logs() as records, pytest.raises(CommandPublishError):
        await NullCommandPublisher().publish(DEVICE_ID, _payload())

    warnings = [record for record in records if record.levelno == logging.WARNING]
    assert len(warnings) == 1
    assert DEVICE_ID in warnings[0].getMessage()
    assert URL not in warnings[0].getMessage()

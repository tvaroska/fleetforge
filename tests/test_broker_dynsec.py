"""The broker-provisioning seam, with **no broker and no database**.

What is provable with no broker is the wire encoding and the state machine — which is
exactly what the pure/IO split in `broker/dynsec.py` exists for. The **live** end-to-end
check is `python -m fleetforge.broker selftest` (`just broker-check`, R0-sec-1): it
provisions two throwaway devices against a running broker and proves the ACL matrix.
It is deliberately not part of `just test`, which must not need a broker.

Two cases here are security properties rather than plumbing:

* `createClient` carries **no `clientid`** (guessing the agent's client id would
  strand boards that enroll and then cannot connect), and its `roles` entry is the
  (deliberately empty) `device` role — the two `%u` pattern ACLs live in
  `mosquitto/acl`, since dynsec cannot express `%u`;
* a reply whose `correlationData` does not match **raises**, because reading a stale
  reply as this command's success hands a device a password the broker never stored.
"""

import json
import logging
from typing import Any

import aiomqtt
import pytest

from fleetforge.broker import (
    DEVICE_ROLE,
    BrokerProvisioningError,
    NullProvisioner,
    generate_broker_password,
)
from fleetforge.broker.dynsec import (
    CREATE_CLIENT,
    DELETE_CLIENT,
    ENABLE_CLIENT,
    SET_CLIENT_PASSWORD,
    DynsecProvisioner,
    DynsecResponse,
    DynsecSession,
    create_client_command,
    delete_client_command,
    enable_client_command,
    encode_command,
    is_already_exists,
    is_not_found,
    parse_response,
    retained_up_topics,
    set_client_password_command,
)
from tests.conftest import capture_logs

DEVICE_ID = "a4cf12b3de90"
PASSWORD = "s3cret-broker-password"  # noqa: S105 - a fixture value, not a credential
CORRELATION = "c0ffee"

# Mosquitto 2.0's actual wording when `createClient` hits a taken username.
ALREADY_EXISTS = "Client already exists"


def _reply(command: str, *, correlation: str = CORRELATION, error: str | None = None) -> bytes:
    entry: dict[str, Any] = {"command": command, "correlationData": correlation}
    if error is not None:
        entry["error"] = error
    return json.dumps({"responses": [entry]}).encode()


# ---------------------------------------------------------------------------
# Pure: the commands on the wire
# ---------------------------------------------------------------------------


def test_create_client_command_shape() -> None:
    """The exact dynsec `createClient`, with the role and no `clientid`."""
    command = create_client_command(DEVICE_ID, PASSWORD, DEVICE_ROLE, CORRELATION)

    assert command == {
        "command": CREATE_CLIENT,
        "username": DEVICE_ID,
        "password": PASSWORD,
        "roles": [{"rolename": "device"}],
        "correlationData": CORRELATION,
    }
    # `spec/device-protocol.md` does not pin the agent's MQTT client id and R0-fw-1 is
    # unwritten, so binding one would strand every board that guessed differently.
    assert "clientid" not in command


def test_username_is_the_device_id_verbatim() -> None:
    """The `%u` pattern ACLs bind to the username; it is never normalised here."""
    for command in (
        create_client_command(DEVICE_ID, PASSWORD, DEVICE_ROLE, CORRELATION),
        set_client_password_command(DEVICE_ID, PASSWORD, CORRELATION),
        enable_client_command(DEVICE_ID, CORRELATION),
    ):
        assert command["username"] == DEVICE_ID


def test_encode_command_wraps_in_the_commands_envelope() -> None:
    payload = json.loads(encode_command(enable_client_command(DEVICE_ID, CORRELATION)))
    assert payload == {
        "commands": [
            {
                "command": ENABLE_CLIENT,
                "username": DEVICE_ID,
                "correlationData": CORRELATION,
            }
        ]
    }


# ---------------------------------------------------------------------------
# Pure: reading the reply
# ---------------------------------------------------------------------------


def test_parse_response_success() -> None:
    response = parse_response(_reply(CREATE_CLIENT), expect=CREATE_CLIENT, correlation=CORRELATION)
    assert response == DynsecResponse(command=CREATE_CLIENT, error=None)


def test_parse_response_returns_an_error_rather_than_raising() -> None:
    """An error is a key in a 200-shaped body — "already exists" is a branch."""
    response = parse_response(
        _reply(CREATE_CLIENT, error=ALREADY_EXISTS),
        expect=CREATE_CLIENT,
        correlation=CORRELATION,
    )
    assert response.error == ALREADY_EXISTS


@pytest.mark.parametrize(
    ("raw", "why"),
    [
        (b"not json at all", "garbage bytes"),
        (b"[]", "an array where an object belongs"),
        (b'{"responses": []}', "an empty responses array"),
        (b'{"responses": "nope"}', "responses is not an array"),
        (b'{"responses": ["nope"]}', "the entry is not an object"),
    ],
)
def test_parse_response_raises_on_unusable_bodies(raw: bytes, why: str) -> None:
    with pytest.raises(BrokerProvisioningError):
        parse_response(raw, expect=CREATE_CLIENT, correlation=CORRELATION)


def test_parse_response_raises_on_the_wrong_command() -> None:
    with pytest.raises(BrokerProvisioningError):
        parse_response(_reply(ENABLE_CLIENT), expect=CREATE_CLIENT, correlation=CORRELATION)


def test_parse_response_raises_on_a_stale_correlation() -> None:
    """A leftover reply from a timed-out command must never read as success."""
    with pytest.raises(BrokerProvisioningError):
        parse_response(
            _reply(CREATE_CLIENT, correlation="somebody-elses"),
            expect=CREATE_CLIENT,
            correlation=CORRELATION,
        )


def test_is_already_exists() -> None:
    assert is_already_exists(ALREADY_EXISTS)
    assert is_already_exists("CLIENT ALREADY EXISTS")
    assert not is_already_exists("Invalid role name")


# ---------------------------------------------------------------------------
# The provisioners
# ---------------------------------------------------------------------------


class FakeDynsecSession:
    """Records commands and answers them from a scripted list of replies."""

    def __init__(self, replies: list[bytes | Exception]) -> None:
        self.replies = list(replies)
        self.commands: list[dict[str, Any]] = []

    async def command(
        self, payload: dict[str, Any], *, expect: str, correlation: str
    ) -> DynsecResponse:
        self.commands.append(payload)
        reply = self.replies.pop(0)
        if isinstance(reply, Exception):
            raise reply
        # The broker echoes the correlation the caller generated.
        body = json.loads(reply)
        body["responses"][0]["correlationData"] = correlation
        return parse_response(json.dumps(body).encode(), expect=expect, correlation=correlation)

    def __call__(self) -> "FakeDynsecSession":
        return self

    async def __aenter__(self) -> DynsecSession:
        return self

    async def __aexit__(self, *exc_info: object) -> None:
        return None


def _provisioner(session: FakeDynsecSession) -> DynsecProvisioner:
    return DynsecProvisioner(session, role=DEVICE_ROLE, timeout_s=5.0)


async def test_ensure_client_creates_in_one_command() -> None:
    session = FakeDynsecSession([_reply(CREATE_CLIENT)])

    assert await _provisioner(session).ensure_client(DEVICE_ID, PASSWORD) is True
    assert [command["command"] for command in session.commands] == [CREATE_CLIENT]


async def test_ensure_client_is_idempotent_when_the_username_exists() -> None:
    """The grace-window retry and every reconcile land here: create → set → enable."""
    session = FakeDynsecSession(
        [
            _reply(CREATE_CLIENT, error=ALREADY_EXISTS),
            _reply(SET_CLIENT_PASSWORD),
            _reply(ENABLE_CLIENT),
        ]
    )

    assert await _provisioner(session).ensure_client(DEVICE_ID, PASSWORD) is True
    assert [command["command"] for command in session.commands] == [
        CREATE_CLIENT,
        SET_CLIENT_PASSWORD,
        ENABLE_CLIENT,
    ]
    assert session.commands[1]["password"] == PASSWORD


async def test_ensure_client_raises_on_any_other_error() -> None:
    session = FakeDynsecSession([_reply(CREATE_CLIENT, error="Invalid role name")])

    with pytest.raises(BrokerProvisioningError):
        await _provisioner(session).ensure_client(DEVICE_ID, PASSWORD)


async def test_ensure_client_raises_when_a_later_command_is_refused() -> None:
    """A create that says "exists" and a setClientPassword that fails is still a 503."""
    session = FakeDynsecSession(
        [
            _reply(CREATE_CLIENT, error=ALREADY_EXISTS),
            _reply(SET_CLIENT_PASSWORD, error="Client not found"),
        ]
    )

    with pytest.raises(BrokerProvisioningError):
        await _provisioner(session).ensure_client(DEVICE_ID, PASSWORD)


@pytest.mark.parametrize(
    "failure", [TimeoutError(), aiomqtt.MqttError("broker gone"), OSError("connection refused")]
)
async def test_transport_failures_become_broker_provisioning_errors(failure: Exception) -> None:
    """No `aiomqtt` exception may escape into the request — the router catches one type."""
    session = FakeDynsecSession([failure])

    with pytest.raises(BrokerProvisioningError):
        await _provisioner(session).ensure_client(DEVICE_ID, PASSWORD)


async def test_null_provisioner_provisions_nothing_and_says_so() -> None:
    """`False`, so the caller leaves `broker_provisioned_at` NULL — and no password in the log."""
    with capture_logs() as records:
        assert await NullProvisioner().ensure_client(DEVICE_ID, PASSWORD) is False

    warnings = [record for record in records if record.levelno == logging.WARNING]
    assert len(warnings) == 1
    assert DEVICE_ID in warnings[0].getMessage()
    assert PASSWORD not in warnings[0].getMessage()


def test_generated_passwords_are_long_and_unique() -> None:
    passwords = {generate_broker_password() for _ in range(20)}
    assert len(passwords) == 20
    assert all(len(password) >= 32 for password in passwords)


def test_delete_client_command_and_retained_up_topics() -> None:
    command = delete_client_command(DEVICE_ID, CORRELATION)
    assert command == {
        "command": DELETE_CLIENT,
        "username": DEVICE_ID,
        "correlationData": CORRELATION,
    }
    assert retained_up_topics(DEVICE_ID) == (
        f"ff/v1/d/{DEVICE_ID}/up/announce",
        f"ff/v1/d/{DEVICE_ID}/up/presence",
        f"ff/v1/d/{DEVICE_ID}/up/status",
    )
    assert is_not_found("Client not found")
    assert not is_not_found("Permission denied")


async def test_remove_client_is_idempotent_when_found_or_already_absent() -> None:
    session = FakeDynsecSession([_reply(DELETE_CLIENT)])
    assert await _provisioner(session).remove_client(DEVICE_ID) is True
    assert [c["command"] for c in session.commands] == [DELETE_CLIENT]

    absent = FakeDynsecSession([_reply(DELETE_CLIENT, error="Client not found")])
    assert await _provisioner(absent).remove_client(DEVICE_ID) is True
    assert [c["command"] for c in absent.commands] == [DELETE_CLIENT]


async def test_remove_client_raises_on_unexpected_dynsec_error() -> None:
    session = FakeDynsecSession([_reply(DELETE_CLIENT, error="Permission denied")])
    with pytest.raises(BrokerProvisioningError):
        await _provisioner(session).remove_client(DEVICE_ID)


async def test_null_provisioner_remove_client_logs_warning_and_returns_false() -> None:
    with capture_logs() as records:
        assert await NullProvisioner().remove_client(DEVICE_ID) is False
    warnings = [r for r in records if r.levelno == logging.WARNING]
    assert len(warnings) == 1
    assert DEVICE_ID in warnings[0].getMessage()


def test_decommission_routes_and_include_decommissioned_query_are_registered() -> None:
    from fleetforge.api.main import create_app

    schema = create_app().openapi()
    assert "/v1/devices/{device_id}/decommission" in schema["paths"]
    assert "post" in schema["paths"]["/v1/devices/{device_id}/decommission"]
    assert "delete" in schema["paths"]["/v1/devices/{device_id}"]
    list_params = {
        param["name"] for param in schema["paths"]["/v1/devices"]["get"].get("parameters", [])
    }
    assert "include_decommissioned" in list_params


async def test_enroll_device_upsert_resets_transient_health_and_cfg_columns() -> None:
    import datetime as dt
    import uuid
    from unittest.mock import AsyncMock, MagicMock

    from sqlalchemy.dialects import postgresql

    from fleetforge.db.models import Device
    from fleetforge.registry import enroll_device

    captured_sql: list[str] = []
    captured_params: list[dict[str, Any]] = []

    class _FakeScalarResult:
        def one(self) -> Device:
            return Device(device_id=DEVICE_ID)

    async def _fake_scalars(stmt: Any, **kwargs: Any) -> _FakeScalarResult:
        compiled = stmt.compile(
            dialect=postgresql.dialect(),
            compile_kwargs={"literal_binds": False},
        )
        captured_sql.append(str(compiled))
        captured_params.append(dict(compiled.params or {}))
        return _FakeScalarResult()

    fake_session = MagicMock()
    fake_session.scalars = AsyncMock(side_effect=_fake_scalars)
    fake_session.flush = AsyncMock()

    await enroll_device(
        fake_session,
        device_id=DEVICE_ID,
        identity={"platform_type": "esp32c6", "link_type": "wifi", "power_class": "always_on"},
        project_id=uuid.UUID("00000000-0000-4000-8000-000000000001"),
        group_id=None,
        enrolled_at=dt.datetime.now(dt.UTC),
        mqtt_password_hash="argon2-hash",
    )

    assert len(captured_sql) == 1
    sql = captured_sql[0]
    for fragment in (
        "decommissioned_at = %(",
        "broker_provisioned_at = %(",
        "presence_reported = %(",
        "last_reset_reason = %(",
        "health_at = %(",
        "uptime_s = %(",
        "rssi = %(",
        "free_heap = %(",
        "min_free_heap = %(",
        "boot_ok = %(",
        "secrets_rev = %(",
        "stay_awake = %(",
        "is_dev = CASE WHEN (devices.project_id = excluded.project_id AND devices.decommissioned_at IS NULL) THEN devices.is_dev",
        "group_id = CASE WHEN (devices.project_id = excluded.project_id AND devices.decommissioned_at IS NULL) THEN coalesce(excluded.group_id, devices.group_id)",
    ):
        assert fragment in sql, f"missing {fragment!r} in {sql}"

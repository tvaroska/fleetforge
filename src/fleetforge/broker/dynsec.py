"""Mosquitto dynamic-security over MQTT — the real `BrokerProvisioner`. **CRITICAL.**

Dynsec is **authentication only**: it decides who exists and what their password is.
What a device may *do* is `mosquitto/acl`'s two `%u` pattern rules, because the plugin
has no `%u` substitution (DECISIONS.md 2026-09-08, R0-sec-1). Both backends are
consulted by the broker and allow wins.

Pure command building and response parsing are split from the I/O, the same way
`ingestor/protocol.py` is split from `ingestor/main.py`: it is what lets
`tests/test_broker_dynsec.py` cover the wire encoding with **no broker running**.

Five properties of the dynsec protocol that are easy to get wrong, all of which are
encoded below (verified against Mosquitto 2.0.x):

1. Commands are published to `$CONTROL/dynamic-security/v1` as
   `{"commands": [ … ]}` and the reply arrives on
   `$CONTROL/dynamic-security/v1/response` — **only to the client that issued the
   command**. Subscribe *before* publishing, or the reply is already gone.
2. **An error is a key in a 200-shaped body**, never a transport error:
   `{"responses":[{"command":"createClient","error":"Client already exists"}]}`.
   `parse_response` therefore returns the error rather than raising on it — the
   caller decides, because "already exists" is a branch, not a failure.
3. `correlationData` is echoed back and **must be matched**. Without the check, a
   stale reply from a previously timed-out command is read as this one's success —
   i.e. a device gets a password the broker never stored.
4. `createClient` on an existing username errors, so the idempotent shape is
   *create → on already-exists → `setClientPassword` + `enableClient`*.
5. The dynsec control client id carries a **random suffix**. Two API workers (or two
   concurrent enrollments) sharing one client id kick each other off the broker
   mid-command, and the symptom is an intermittent 503.

`clientid` is deliberately **not** bound to the created client: `spec/device-protocol.md`
does not say what MQTT client id the agent uses and `R0-fw-1` is unwritten, so guessing
would strand boards that enroll and then cannot connect. Proposed as a spec addition
instead (R0-be-4 hand-off).
"""

import asyncio
import contextlib
import json
import logging
import uuid
from collections.abc import AsyncIterator, Callable
from contextlib import AbstractAsyncContextManager
from dataclasses import dataclass
from typing import Any, Protocol

import aiomqtt

from fleetforge.broker.provisioner import BrokerProvisioningError
from fleetforge.config import Settings

logger = logging.getLogger(__name__)

DYNSEC_CONTROL_TOPIC = "$CONTROL/dynamic-security/v1"
DYNSEC_RESPONSE_TOPIC = "$CONTROL/dynamic-security/v1/response"

CREATE_CLIENT = "createClient"
SET_CLIENT_PASSWORD = "setClientPassword"  # noqa: S105 - a dynsec command name
ENABLE_CLIENT = "enableClient"

# Mosquitto's wording for "that username is taken". Version-fragile on purpose: if an
# upgrade changes the phrasing, `ensure_client` raises `BrokerProvisioningError` and
# the enrollment answers 503 — loud, rather than silently leaving a client with the
# wrong password. That is the right direction to fail in.
ALREADY_EXISTS_MARKER = "already exists"


@dataclass(frozen=True, slots=True)
class DynsecResponse:
    """One entry of the broker's `responses` array. `error` present means it refused."""

    command: str
    error: str | None


def new_correlation() -> str:
    """A fresh `correlationData` value for one command."""
    return uuid.uuid4().hex


def create_client_command(
    username: str, password: str, role: str, correlation: str
) -> dict[str, Any]:
    """`createClient` for `username` with `password` and exactly one role.

    `username` is the device id, unnormalised: the `%u` pattern ACLs in `mosquitto/acl`
    bind to it (`broker/provisioner.py`). `role` is deliberately empty — it exists only
    because this command requires a role name. No `clientid` key — see the module
    docstring.
    """
    return {
        "command": CREATE_CLIENT,
        "username": username,
        "password": password,
        "roles": [{"rolename": role}],
        "correlationData": correlation,
    }


def set_client_password_command(username: str, password: str, correlation: str) -> dict[str, Any]:
    """`setClientPassword` — the idempotent half, for a username that already exists."""
    return {
        "command": SET_CLIENT_PASSWORD,
        "username": username,
        "password": password,
        "correlationData": correlation,
    }


def enable_client_command(username: str, correlation: str) -> dict[str, Any]:
    """`enableClient` — a previously disabled client must not stay disabled."""
    return {
        "command": ENABLE_CLIENT,
        "username": username,
        "correlationData": correlation,
    }


def encode_command(command: dict[str, Any]) -> bytes:
    """Wrap one command in dynsec's `{"commands": [...]}` envelope."""
    return json.dumps({"commands": [command]}).encode("utf-8")


def parse_response(raw: bytes, *, expect: str, correlation: str) -> DynsecResponse:
    """Read one dynsec reply, or raise `BrokerProvisioningError`.

    Raises on anything that means "this is not the answer to my command": undecodable
    JSON, a missing or empty `responses` array, a different `command`, or a
    `correlationData` that is not `correlation`. It does **not** raise on a present
    `error` — the caller branches on it.
    """
    try:
        body = json.loads(raw)
    except (json.JSONDecodeError, UnicodeDecodeError) as exc:
        raise BrokerProvisioningError(f"undecodable dynsec response: {type(exc).__name__}") from exc

    if not isinstance(body, dict):
        raise BrokerProvisioningError("dynsec response is not an object")

    responses = body.get("responses")
    if not isinstance(responses, list) or not responses:
        raise BrokerProvisioningError("dynsec response carries no responses array")

    entry = responses[0]
    if not isinstance(entry, dict):
        raise BrokerProvisioningError("dynsec response entry is not an object")

    command = entry.get("command")
    if command != expect:
        raise BrokerProvisioningError(f"dynsec response is for {command!r}, expected {expect!r}")

    echoed = entry.get("correlationData")
    if echoed != correlation:
        # A stale reply from a timed-out command. Reading it as this command's
        # success would hand a device a password the broker never stored.
        raise BrokerProvisioningError(f"dynsec response for {expect} has a stale correlationData")

    error = entry.get("error")
    return DynsecResponse(command=command, error=str(error) if error is not None else None)


def is_already_exists(error: str) -> bool:
    """Does `error` mean "that username is taken"? Version-fragile — see the marker."""
    return ALREADY_EXISTS_MARKER in error.lower()


class DynsecSession(Protocol):
    """One request/response round-trip against the dynsec control topic."""

    async def command(
        self, payload: dict[str, Any], *, expect: str, correlation: str
    ) -> DynsecResponse: ...


class MqttDynsecSession:
    """A dynsec control session over one connected `aiomqtt.Client`.

    The client is already subscribed to `DYNSEC_RESPONSE_TOPIC` when this is
    constructed (`mqtt_dynsec_session` does it before yielding), because the reply
    goes only to the issuing client and a subscribe-after-publish loses it.
    """

    def __init__(self, client: aiomqtt.Client, messages: AsyncIterator[aiomqtt.Message]) -> None:
        self._client = client
        self._messages = messages

    async def command(
        self, payload: dict[str, Any], *, expect: str, correlation: str
    ) -> DynsecResponse:
        """Publish one command and return its parsed reply.

        Replies that do not match `correlation` are skipped rather than treated as the
        answer — a control topic can carry another worker's leftovers.
        """
        await self._client.publish(DYNSEC_CONTROL_TOPIC, encode_command(payload), qos=1)
        async for message in self._messages:
            raw = message.payload if isinstance(message.payload, bytes | bytearray) else b""
            body = bytes(raw)
            if correlation.encode("utf-8") not in body:
                logger.debug("skipping a dynsec reply that is not ours")
                continue
            return parse_response(body, expect=expect, correlation=correlation)
        raise BrokerProvisioningError(f"dynsec connection closed while awaiting {expect}")


@contextlib.asynccontextmanager
async def mqtt_dynsec_session(settings: Settings) -> AsyncIterator[DynsecSession]:
    """One short-lived dynsec control connection.

    **One connection per call, deliberately not a long-lived client.** Enrollment
    happens ~25 times in v1's lifetime (`spec/prd.md` → *Capacity*); a persistent
    control client means one connection per uvicorn worker, reconnect logic, and
    another thing that can be silently disconnected while looking healthy. Please do
    not "optimise" this into a singleton.

    The identifier carries a random suffix: two workers sharing a client id kick each
    other off the broker mid-command, and the symptom is an intermittent 503.
    """
    async with aiomqtt.Client(
        hostname=settings.mqtt_host,
        port=settings.mqtt_port,
        identifier=f"fleetforge-api-dynsec-{uuid.uuid4().hex[:8]}",
        username=settings.mqtt_dynsec_username,
        password=settings.mqtt_dynsec_password,
    ) as client:
        # BEFORE any publish: the response is delivered only to the issuing client.
        await client.subscribe(DYNSEC_RESPONSE_TOPIC, qos=1)
        yield MqttDynsecSession(client, client.messages.__aiter__())


SessionFactory = Callable[[], AbstractAsyncContextManager[DynsecSession]]


class DynsecProvisioner:
    """`BrokerProvisioner` backed by Mosquitto's dynamic-security plugin.

    The session factory is injected so tests drive a fake session with no broker;
    `api/deps.get_broker_provisioner` supplies `mqtt_dynsec_session`.
    """

    def __init__(self, session_factory: SessionFactory, *, role: str, timeout_s: float) -> None:
        self._session_factory = session_factory
        self._role = role
        self._timeout_s = timeout_s

    async def ensure_client(self, device_id: str, password: str) -> bool:
        """Create-or-update `device_id`'s broker client. Idempotent, as the Protocol requires.

        Every exception the transport can produce (`TimeoutError`, `aiomqtt.MqttError`,
        `OSError`) becomes a `BrokerProvisioningError`: the router catches only that,
        and an `aiomqtt` exception escaping into a request handler is a 500 where a
        retriable 503 belongs.
        """
        try:
            async with asyncio.timeout(self._timeout_s), self._session_factory() as session:
                created = await self._create(session, device_id, password)
                if created:
                    return True
                # The username already exists — the grace-window retry, a re-flashed
                # board, or a reconcile. Make the stored credential match anyway.
                await self._run(
                    session,
                    set_client_password_command,
                    SET_CLIENT_PASSWORD,
                    device_id,
                    password,
                )
                await self._run(session, enable_client_command, ENABLE_CLIENT, device_id)
        except TimeoutError as exc:
            raise BrokerProvisioningError(
                f"dynsec timed out after {self._timeout_s}s provisioning {device_id}"
            ) from exc
        except (aiomqtt.MqttError, OSError) as exc:
            raise BrokerProvisioningError(
                f"dynsec transport failure provisioning {device_id}: {type(exc).__name__}"
            ) from exc
        return True

    async def _create(self, session: DynsecSession, device_id: str, password: str) -> bool:
        """`createClient`; `False` means "already exists", anything else raises."""
        correlation = new_correlation()
        response = await session.command(
            create_client_command(device_id, password, self._role, correlation),
            expect=CREATE_CLIENT,
            correlation=correlation,
        )
        if response.error is None:
            return True
        if is_already_exists(response.error):
            return False
        raise BrokerProvisioningError(f"dynsec {CREATE_CLIENT} refused: {response.error}")

    async def _run(
        self,
        session: DynsecSession,
        builder: Callable[..., dict[str, Any]],
        expect: str,
        *args: str,
    ) -> None:
        """Run one command that has no acceptable error."""
        correlation = new_correlation()
        response = await session.command(
            builder(*args, correlation), expect=expect, correlation=correlation
        )
        if response.error is not None:
            raise BrokerProvisioningError(f"dynsec {expect} refused: {response.error}")

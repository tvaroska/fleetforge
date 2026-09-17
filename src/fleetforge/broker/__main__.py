"""`python -m fleetforge.broker selftest` — the live broker ACL matrix. **CRITICAL.**

```
just broker-check                                  # against MQTT_HOST/MQTT_PORT
just broker-check --host localhost --port 8883     # force the coordinates
docker compose exec -T api python -m fleetforge.broker selftest
```

Both the T2 harness for `R0-sec-1` and the ops answer to "is broker authz still what
we think it is?". It provisions two throwaway devices through the **real**
`DynsecProvisioner` and then proves, against the running broker, the property
`CRITICAL.md` names: *a device can only ever be itself* — and, since R1-be-2, that the
API's `commander` credential can command exactly one board at a time and can forge no
telemetry (`MQTT_COMMAND_USERNAME`/`_PASSWORD` must be set, or the run fails: an unset
pair is the configuration in which every deploy answers 503).

Three things about how the assertions are written, all of which are the difference
between a test that proves something and one that does not:

1. **Read authorisation is enforced on DELIVERY, not on SUBSCRIBE.** With `acl_file`
   a device may subscribe to `#` and gets SUBACK 0. Asserting on the SUBACK code
   would pass while proving nothing, so every read assertion here is about what a
   subscriber *actually received*.
2. **A denied publish is invisible below MQTT v5.** aiomqtt/paho do not surface the
   PUBACK reason code (`Client.publish` checks the local `rc`, not the ack), and in
   v3.1.1 there is no reason code at all — a denied publish "succeeds" and is
   silently dropped. So a denial is asserted as **non-delivery to a watcher that
   would otherwise have seen it**, waiting the full timeout every time. For the raw
   reason code use `mosquitto_pub -V 5 -d` from inside the broker container (RC:135).
3. **`$CONTROL` cannot be watched** — `#` does not match a `$`-prefixed topic. The
   fleet-takeover check is therefore end-to-end: the device publishes a real
   `createClient` command and the selftest then proves that client does not exist by
   failing to connect as it.

The throwaway device ids are `ffff…`, outside any real eFuse MAC in the fleet, and
re-running is safe (`ensure_client` is create → already-exists → setClientPassword).
They are deliberately **not** deleted: `deleteClient` is not in the `BrokerProvisioner`
seam yet (R0-be-4 follow-up), and inventing it here would widen a CRITICAL surface for
a test's convenience.

**Prints no password.** Same argparse / stdout-is-the-transcript shape as
`storage/__main__.py`.
"""

import argparse
import asyncio
import contextlib
import sys
import time
import uuid
from collections.abc import AsyncIterator

import aiomqtt

from fleetforge.broker.dynsec import (
    DYNSEC_CONTROL_TOPIC,
    DynsecProvisioner,
    create_client_command,
    encode_command,
    mqtt_dynsec_session,
    new_correlation,
)
from fleetforge.broker.provisioner import generate_broker_password
from fleetforge.config import Settings

# Outside the real fleet: an eFuse MAC never starts `ffff`.
DEVICE_A = "ffff00000001"
DEVICE_B = "ffff00000002"
# The client a compromised device would try to mint for itself over $CONTROL.
IMPOSTOR = "ffff0000dead"

# Deliberately wrong: the broker must refuse a real username with a bad password, not
# just an unknown one. Not a credential.
WRONG_PASSWORD = "not-the-password"  # noqa: S105 - a value the broker must reject

# Every "must not arrive" assertion waits this out in full, so it has to stay small.
DELIVERY_TIMEOUT_S = 3.0
# How long the broker gets to apply a $CONTROL command before we check it did not.
CONTROL_SETTLE_S = 1.0


def _step(message: str) -> None:
    """One line per step, to stdout, so the whole run reads as a transcript."""
    print(message, flush=True)


def _identifier(role: str) -> str:
    """A unique client id per connection: two clients sharing one kick each other off."""
    return f"ff-selftest-{role}-{uuid.uuid4().hex[:8]}"


class _Inbox:
    """What one subscriber actually received — the only honest read-ACL evidence."""

    def __init__(self) -> None:
        self.topics: list[str] = []


@contextlib.asynccontextmanager
async def _watching(client: aiomqtt.Client, topic_filter: str) -> AsyncIterator[_Inbox]:
    """Subscribe and drain into an `_Inbox` for the life of the block.

    The subscribe itself is expected to be *granted* even for a filter the client may
    not read (property 1 in the module docstring) — that is not a finding.
    """
    await client.subscribe(topic_filter, qos=1)
    inbox = _Inbox()

    async def pump() -> None:
        async for message in client.messages:
            inbox.topics.append(message.topic.value)

    task = asyncio.create_task(pump())
    try:
        yield inbox
    finally:
        task.cancel()
        with contextlib.suppress(asyncio.CancelledError):
            await task


async def _settle(inbox: _Inbox, topic: str, *, expected: bool) -> None:
    """Wait for `topic` to arrive, or wait the full timeout proving it did not."""
    deadline = time.monotonic() + DELIVERY_TIMEOUT_S
    while time.monotonic() < deadline:
        if expected and topic in inbox.topics:
            return
        await asyncio.sleep(0.05)
    if expected:
        raise RuntimeError(f"{topic} was never delivered — the allow rule is broken")
    if topic in inbox.topics:
        raise RuntimeError(f"{topic} WAS delivered — a device can impersonate another")


async def _publish(
    settings: Settings, *, username: str, password: str, topic: str, payload: bytes = b"{}"
) -> None:
    """Publish one QoS-1 message as `username`. A DENIED publish raises nothing here."""
    async with aiomqtt.Client(
        hostname=settings.mqtt_host,
        port=settings.mqtt_port,
        identifier=_identifier("pub"),
        username=username,
        password=password,
    ) as client:
        await client.publish(topic, payload, qos=1)


async def _refused(settings: Settings, *, username: str | None, password: str | None) -> str:
    """Assert the broker refuses this credential at CONNECT; return its wording."""
    try:
        async with aiomqtt.Client(
            hostname=settings.mqtt_host,
            port=settings.mqtt_port,
            identifier=_identifier("authn"),
            username=username,
            password=password,
        ):
            pass
    except aiomqtt.MqttError as exc:
        return str(exc)
    raise RuntimeError(f"the broker ACCEPTED {username or '<anonymous>'} — authentication is off")


async def _check_publish_acl(settings: Settings, device_a: str, pw_a: str, device_b: str) -> None:
    """The write half, watched by the dynsec admin (the one client that may read `#`)."""
    async with (
        aiomqtt.Client(
            hostname=settings.mqtt_host,
            port=settings.mqtt_port,
            identifier=_identifier("watch"),
            username=settings.mqtt_dynsec_username,
            password=settings.mqtt_dynsec_password,
        ) as watcher,
        _watching(watcher, "#") as seen,
    ):
        own_up = f"ff/v1/d/{device_a}/up/hb"
        await _publish(settings, username=device_a, password=pw_a, topic=own_up)
        await _settle(seen, own_up, expected=True)
        _step(f"allow    {device_a} -> {own_up} delivered (mosquitto/acl: pattern write)")

        foreign_up = f"ff/v1/d/{device_b}/up/hb"
        await _publish(settings, username=device_a, password=pw_a, topic=foreign_up)
        await _settle(seen, foreign_up, expected=False)
        _step(f"deny     {device_a} -> {foreign_up} dropped (no impersonation)")

        own_dn = f"ff/v1/d/{device_a}/dn/cmd"
        await _publish(settings, username=device_a, password=pw_a, topic=own_dn)
        await _settle(seen, own_dn, expected=False)
        _step(f"deny     {device_a} -> {own_dn} dropped (dn/ is read-only for a device)")


async def _check_delivery_confidentiality(
    settings: Settings, device_a: str, pw_a: str, device_b: str, pw_b: str
) -> None:
    """Device A subscribes to `#`; B's own traffic must still never reach it."""
    async with (
        aiomqtt.Client(
            hostname=settings.mqtt_host,
            port=settings.mqtt_port,
            identifier=_identifier("dev-a"),
            username=device_a,
            password=pw_a,
        ) as a_client,
        _watching(a_client, "#") as a_seen,
    ):
        foreign_up = f"ff/v1/d/{device_b}/up/hb"
        await _publish(settings, username=device_b, password=pw_b, topic=foreign_up)
        await _settle(a_seen, foreign_up, expected=False)
        _step(
            f"deny     {device_a} subscribed to '#' (SUBACK granted, as expected) and "
            f"received nothing while {device_b} published {foreign_up}"
        )


async def _check_command_delivery(
    settings: Settings, device_a: str, pw_a: str, device_b: str, pw_b: str
) -> None:
    """The `commander` credential (R1-be-2): may send a `dn/` command, to one board only.

    Three properties in one block, because they only mean anything together:

    * a command published by the API's credential **reaches** the addressed device —
      which also proves `mosquitto/acl`'s `pattern read ff/v1/d/%u/dn/#` wins over
      dynsec's default deny on receive, the single fact the whole deploy path rests on;
    * the *other* device, subscribed to `#` at the same moment, receives nothing, so
      `publishClientSend ff/v1/d/+/dn/#` is not a broadcast;
    * the commander itself cannot publish to `up/`, so a credential that leaks out of
      the API cannot forge device telemetry or a fake `up/status` for a deploy.

    Both subscribers are watched with `#` on purpose (property 1 in the module
    docstring): the SUBACK proves nothing, only delivery does.
    """
    if not (settings.mqtt_command_username and settings.mqtt_command_password):
        raise RuntimeError(
            "MQTT_COMMAND_USERNAME / MQTT_COMMAND_PASSWORD are unset, so the deploy "
            "publisher's ACLs cannot be checked — the API would answer 503 to every "
            "deploy in this configuration"
        )
    commander = settings.mqtt_command_username
    commander_pw = settings.mqtt_command_password

    async with (
        aiomqtt.Client(
            hostname=settings.mqtt_host,
            port=settings.mqtt_port,
            identifier=_identifier("dev-a"),
            username=device_a,
            password=pw_a,
        ) as a_client,
        _watching(a_client, "#") as a_seen,
        aiomqtt.Client(
            hostname=settings.mqtt_host,
            port=settings.mqtt_port,
            identifier=_identifier("dev-b"),
            username=device_b,
            password=pw_b,
        ) as b_client,
        _watching(b_client, "#") as b_seen,
    ):
        command_topic = f"ff/v1/d/{device_a}/dn/cmd"
        await _publish(settings, username=commander, password=commander_pw, topic=command_topic)
        await _settle(a_seen, command_topic, expected=True)
        _step(f"allow    {commander} -> {command_topic} delivered to {device_a}")
        await _settle(b_seen, command_topic, expected=False)
        _step(f"deny     {device_b} received nothing while {device_a} was commanded")

    async with (
        aiomqtt.Client(
            hostname=settings.mqtt_host,
            port=settings.mqtt_port,
            identifier=_identifier("watch"),
            username=settings.mqtt_dynsec_username,
            password=settings.mqtt_dynsec_password,
        ) as watcher,
        _watching(watcher, "#") as seen,
    ):
        forged_up = f"ff/v1/d/{device_a}/up/status"
        await _publish(settings, username=commander, password=commander_pw, topic=forged_up)
        await _settle(seen, forged_up, expected=False)
        _step(f"deny     {commander} -> {forged_up} dropped (the commander is write-dn only)")


async def _check_no_fleet_takeover(settings: Settings, device_a: str, pw_a: str) -> None:
    """A device publishing a real `createClient` must not create a client."""
    impostor_password = generate_broker_password()
    await _publish(
        settings,
        username=device_a,
        password=pw_a,
        topic=DYNSEC_CONTROL_TOPIC,
        payload=encode_command(
            create_client_command(
                IMPOSTOR, impostor_password, settings.mqtt_dynsec_role, new_correlation()
            )
        ),
    )
    await asyncio.sleep(CONTROL_SETTLE_S)
    reason = await _refused(settings, username=IMPOSTOR, password=impostor_password)
    _step(
        f"deny     {device_a} -> {DYNSEC_CONTROL_TOPIC} dropped: {IMPOSTOR} does not "
        f"exist ({reason})"
    )


async def _run(args: argparse.Namespace) -> int:
    settings = Settings()  # type: ignore[call-arg]  # values come from the environment
    overrides: dict[str, object] = {}
    if args.host:
        overrides["mqtt_host"] = args.host
    if args.port:
        overrides["mqtt_port"] = args.port
    if overrides:
        settings = settings.model_copy(update=overrides)

    if not (settings.mqtt_dynsec_username and settings.mqtt_dynsec_password):
        raise RuntimeError(
            "MQTT_DYNSEC_USERNAME / MQTT_DYNSEC_PASSWORD are unset, so there is no "
            "control credential to provision with — this selftest needs the dynsec admin"
        )

    device_a, device_b = args.device_id
    _step(f"broker   {settings.mqtt_host}:{settings.mqtt_port}")
    _step(f"admin    {settings.mqtt_dynsec_username} (dynamic-security control client)")
    _step(
        f"role     {settings.mqtt_dynsec_role} — must exist and is deliberately EMPTY; "
        "the two %u pattern ACLs live in mosquitto/acl"
    )
    _step(
        f"command  {settings.mqtt_command_username or '<unset>'} — the deploy publisher "
        "(role `commander`, write-only on ff/v1/d/+/dn/#)"
    )

    provisioner = DynsecProvisioner(
        lambda: mqtt_dynsec_session(settings),
        role=settings.mqtt_dynsec_role,
        timeout_s=settings.broker_command_timeout_s,
    )
    pw_a = generate_broker_password()
    pw_b = generate_broker_password()
    # A createClient that succeeds is also the proof that the role name matches:
    # dynsec refuses a role it does not know, and that is the 503 on every enrollment.
    for device_id, password in ((device_a, pw_a), (device_b, pw_b)):
        if not await provisioner.ensure_client(device_id, password):
            raise RuntimeError(f"{device_id} was not provisioned — is this the Null provisioner?")
        _step(f"provision {device_id} (createClient, role {settings.mqtt_dynsec_role})")

    await _check_publish_acl(settings, device_a, pw_a, device_b)
    await _check_delivery_confidentiality(settings, device_a, pw_a, device_b, pw_b)
    await _check_command_delivery(settings, device_a, pw_a, device_b, pw_b)
    await _check_no_fleet_takeover(settings, device_a, pw_a)

    reason = await _refused(settings, username=None, password=None)
    _step(f"authn    anonymous connect refused ({reason})")
    reason = await _refused(settings, username=device_a, password=WRONG_PASSWORD)
    _step(f"authn    {device_a} with a wrong password refused ({reason})")
    return 0


def main(argv: list[str] | None = None) -> int:
    """Run the matrix. Returns a process exit code; `SELFTEST OK` means success."""
    parser = argparse.ArgumentParser(prog="python -m fleetforge.broker")
    subparsers = parser.add_subparsers(dest="command", required=True)
    selftest = subparsers.add_parser(
        "selftest", help="provision two devices and prove the live broker ACL matrix"
    )
    selftest.add_argument("--host", help="broker host (default: MQTT_HOST)")
    selftest.add_argument("--port", type=int, help="broker port (default: MQTT_PORT)")
    selftest.add_argument(
        "--device-id",
        nargs=2,
        metavar=("A", "B"),
        default=[DEVICE_A, DEVICE_B],
        help=f"the two throwaway device ids (default: {DEVICE_A} {DEVICE_B})",
    )
    args = parser.parse_args(argv)

    try:
        code = asyncio.run(_run(args))
    except (RuntimeError, OSError, ValueError, aiomqtt.MqttError) as exc:
        # BrokerProvisioningError is a RuntimeError; aiomqtt.MqttError is neither, and
        # an unexpected refusal at connect must read as a failed check, not a traceback.
        print(f"SELFTEST FAILED: {type(exc).__name__}: {exc}", file=sys.stderr)
        return 1
    print("SELFTEST OK")
    return code


if __name__ == "__main__":
    raise SystemExit(main())

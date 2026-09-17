"""The real `CommandPublisher`: one short-lived aiomqtt connection per command.

The adapter half of `broker/commands.py`, in the same relationship `broker/dynsec.py`
has to `broker/provisioner.py` — everything pure lives there, everything that touches a
socket lives here, so `tests/test_broker_commands.py` can drive a whole publish through
an injected fake client with **no broker running**.

Four properties, each of which fails silently rather than loudly when it is wrong:

1. **One connection per publish, not a long-lived client.** A deploy is rare
   (`spec/prd.md` → *Capacity*: 25 devices in v1); a persistent client means one
   connection per uvicorn worker plus reconnect logic the API has nowhere else. Same
   decision, same reasoning, as `mqtt_dynsec_session`. Please do not "optimise" it into
   a singleton.
2. **A random client-id suffix.** Two workers sharing an MQTT client id kick each other
   off the broker mid-command, and the symptom is an intermittent 503.
3. **`clean_session=True` for the publisher.** Durability belongs to the *device's*
   session (`clean_session=false`, `spec/device-protocol.md` → *Retain and durability*);
   the API never subscribes and has no session worth resuming.
4. **`retain=False` is a literal, not a parameter.** A retained `dn/cmd` re-stages on
   every reconnect, forever, so no caller is given the chance to pass the wrong flag.

And the limitation `broker/commands.py` documents at length: under MQTT 3.1.1 the broker
silently drops a publish it denies and the PUBACK carries no reason code, so a return
from `publish()` proves the packet was accepted, never that the device received it.
"""

import asyncio
import logging
import uuid
from collections.abc import Callable
from contextlib import AbstractAsyncContextManager
from typing import Any

import aiomqtt

from fleetforge.broker.commands import CommandPublishError, command_topic, encode_command
from fleetforge.config import Settings

logger = logging.getLogger(__name__)

# `spec/device-protocol.md` → *Topic namespace*: `dn/cmd` is QoS 1, retain **never**.
QOS = 1
RETAIN = False

ClientFactory = Callable[[], AbstractAsyncContextManager[aiomqtt.Client]]


def mqtt_command_client(settings: Settings) -> aiomqtt.Client:
    """A fresh publishing client — properties 1, 2 and 3 of the module docstring.

    The credential is the `commander` one (`MQTT_COMMAND_USERNAME`/`_PASSWORD`), which
    `mosquitto/bootstrap.sh` grants `publishClientSend ff/v1/d/+/dn/#` and nothing else.
    Deliberately **not** the dynsec admin: broker-root is a control-plane credential
    whose rights are over `$CONTROL/...`, and reusing it here would both fail (it has no
    `ff/v1` rights) and blur two privileges that rotate separately.
    """
    return aiomqtt.Client(
        hostname=settings.mqtt_host,
        port=settings.mqtt_port,
        identifier=f"fleetforge-cmd-{uuid.uuid4().hex[:8]}",
        username=settings.mqtt_command_username,
        password=settings.mqtt_command_password,
        clean_session=True,
    )


class MqttCommandPublisher:
    """`CommandPublisher` backed by a real MQTT connection.

    The client factory is injected so tests drive a fake with no broker;
    `api/deps.get_command_publisher` supplies `mqtt_command_client`.
    """

    def __init__(self, client_factory: ClientFactory, *, timeout_s: float) -> None:
        self._client_factory = client_factory
        self._timeout_s = timeout_s

    async def publish(self, device_id: str, payload: dict[str, Any]) -> None:
        """Publish one command to `ff/v1/d/{device_id}/dn/cmd`, or raise.

        Every transport exception (`TimeoutError`, `aiomqtt.MqttError`, `OSError`)
        becomes one `CommandPublishError`: the router catches only that, and an aiomqtt
        exception escaping into a handler is a 500 where a retriable 503 belongs.

        Nothing about `payload` reaches the log except its `id` and `type` — it carries
        the signed URL, which is a bearer credential.
        """
        topic = command_topic(device_id)
        try:
            async with asyncio.timeout(self._timeout_s), self._client_factory() as client:
                await client.publish(topic, encode_command(payload), qos=QOS, retain=RETAIN)
        except TimeoutError as exc:
            raise CommandPublishError(
                f"the broker did not accept the command for {device_id} within {self._timeout_s}s"
            ) from exc
        except (aiomqtt.MqttError, OSError) as exc:
            raise CommandPublishError(
                f"broker transport failure publishing to {device_id}: {type(exc).__name__}"
            ) from exc
        logger.info(
            "published %s command %s to %s (qos %d, retain %s)",
            payload.get("type"),
            payload.get("id"),
            topic,
            QOS,
            RETAIN,
        )

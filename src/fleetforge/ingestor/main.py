"""MQTT ingestor — the SOLE MQTT subscriber (`design/production.md`).

R0-infra-1 ships connect + subscribe + log. R0-be-3 adds payload handling,
derived presence, Postgres writes and `NOTIFY`. **This process must never run with
more than one instance**: N subscribers would ingest every message N times and
split the SSE audience. There is deliberately no database access here yet.

Run it with `python -m fleetforge.ingestor.main`.
"""

import asyncio
import contextlib
import logging
import os
import signal
import time
from pathlib import Path

import aiomqtt

from fleetforge.config import get_settings

logger = logging.getLogger("fleetforge.ingestor")

# Every device-to-server topic. `spec/device-protocol.md` → *Topic namespace*:
# `ff/v1/d/{device_id}/up/{channel}`; the up channels are QoS 1.
UP_TOPIC_FILTER = "ff/v1/d/+/up/#"
UP_TOPIC_QOS = 1

# A stable client id plus `clean_session=False` means the broker keeps this
# subscription and any undelivered QoS-1 messages across an ingestor restart —
# the same durability property the device side relies on.
CLIENT_ID = "fleetforge-ingestor"

# Liveness for a process with no HTTP server: touch a file, and let the container
# healthcheck compare its mtime against now. R0-be-3 inherits this.
HEARTBEAT_PATH = Path(os.environ.get("INGESTOR_HEARTBEAT_FILE", "/tmp/ingestor-alive"))  # noqa: S108

RECONNECT_INITIAL_DELAY = 1.0
RECONNECT_MAX_DELAY = 30.0


def touch_heartbeat() -> None:
    """Bump the liveness file's mtime, never failing the caller."""
    try:
        HEARTBEAT_PATH.touch()
        os.utime(HEARTBEAT_PATH, (time.time(), time.time()))
    except OSError as exc:  # pragma: no cover - a full or read-only /tmp
        logger.warning("heartbeat: cannot touch %s: %s", HEARTBEAT_PATH, exc)


async def handle_message(message: aiomqtt.Message) -> None:
    """Handle one inbound device message.

    R0-infra-1 only observes. Payload *length* is logged, never the payload body:
    logs are not a data store, and telemetry bodies get large from R3.
    """
    payload = message.payload
    size = len(payload) if isinstance(payload, bytes | bytearray | str) else 0
    logger.info("mqtt rx topic=%s qos=%s bytes=%d", message.topic.value, message.qos, size)
    touch_heartbeat()


async def run_once(hostname: str, port: int) -> None:
    """Connect, subscribe and consume until the connection drops."""
    async with aiomqtt.Client(
        hostname=hostname,
        port=port,
        identifier=CLIENT_ID,
        clean_session=False,
    ) as client:
        logger.info("connected to broker %s:%d as %s", hostname, port, CLIENT_ID)
        await client.subscribe(UP_TOPIC_FILTER, qos=UP_TOPIC_QOS)
        logger.info("subscribed to %s (qos %d)", UP_TOPIC_FILTER, UP_TOPIC_QOS)
        touch_heartbeat()
        async for message in client.messages:
            await handle_message(message)


async def run(hostname: str, port: int) -> None:
    """Consume forever, reconnecting with capped exponential backoff.

    A broker restart must never take the ingestor down with it: an ingestor that
    exits on a dropped connection turns a five-second broker blip into a
    fleet-visibility outage that lasts until someone notices the container.
    """
    delay = RECONNECT_INITIAL_DELAY
    while True:
        try:
            await run_once(hostname, port)
            logger.warning("broker connection closed; reconnecting")
        except aiomqtt.MqttError as exc:
            logger.warning("broker error (%s); reconnecting in %.0fs", exc, delay)
        except OSError as exc:
            logger.warning("broker unreachable (%s); reconnecting in %.0fs", exc, delay)
        else:
            delay = RECONNECT_INITIAL_DELAY
        await asyncio.sleep(delay)
        delay = min(delay * 2, RECONNECT_MAX_DELAY)


async def main() -> None:
    """Entrypoint: wire SIGTERM/SIGINT to a clean shutdown, then run."""
    logging.basicConfig(
        level=logging.INFO,
        format='{"ts":"%(asctime)s","level":"%(levelname)s","logger":"%(name)s","msg":"%(message)s"}',
        datefmt="%Y-%m-%dT%H:%M:%S%z",
        force=True,
    )
    settings = get_settings()

    loop = asyncio.get_running_loop()
    task = asyncio.create_task(run(settings.mqtt_host, settings.mqtt_port))

    # `docker compose down` sends SIGTERM; cancelling the task lets aiomqtt send a
    # proper DISCONNECT instead of leaving a half-open session on the broker.
    for sig in (signal.SIGTERM, signal.SIGINT):
        with contextlib.suppress(NotImplementedError):
            loop.add_signal_handler(sig, task.cancel)

    with contextlib.suppress(asyncio.CancelledError):
        await task
    logger.info("ingestor stopped")


if __name__ == "__main__":
    asyncio.run(main())

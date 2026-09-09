"""MQTT ingestor — the SOLE MQTT subscriber (`design/production.md`).

It subscribes to every `up/` topic, derives presence from what arrives, writes the
device row and emits one `ff_events` notification per ingested message; the API
workers `LISTEN` on that channel and fan out over SSE (R0-be-5). **This process must
never run with more than one instance**: N subscribers would ingest every message N
times and split the SSE audience.

Three things it deliberately does not do. It never `INSERT`s a device — the only way
into the registry is a burned enrollment token (`ingestor/store.py`). It never
publishes to `dn/*` — commands come from the API. And it never runs migrations
(`RUN_MIGRATIONS` is unset for this service in `docker-compose.yml`: two processes
racing `alembic upgrade head` is a deadlock waiting for a slow migration), so it
connects to a database the api has already migrated.

`DATABASE_URL` is mandatory here: `get_settings()` raises and the container exits
with a readable message. That is the opposite of `create_app()`'s deliberate
tolerance — the api must still serve `/v1/healthz` to say why it is unhappy, while an
ingestor with no database has nothing to offer.

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
from sqlalchemy.exc import SQLAlchemyError
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from fleetforge.clock import now_utc
from fleetforge.config import get_settings
from fleetforge.db.base import get_engine, get_sessionmaker
from fleetforge.ingestor.handlers import handle_up_message

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
# healthcheck compare its mtime against now.
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


async def handle_message(
    message: aiomqtt.Message,
    sessionmaker: async_sessionmaker[AsyncSession],
    tolerance: float,
) -> None:
    """Handle one inbound device message; never raise.

    Payload *length* is logged, never the payload body: logs are not a data store, and
    telemetry bodies get large from R3.

    One message must not kill the process, and neither must a database outage — the
    heartbeat file is therefore touched even when the write failed. Liveness here
    means "connected to the broker and consuming"; restarting the container does not
    fix Postgres, and a crash-loop would only add reconnect churn to the outage.
    There is no manual ack (paho acks a QoS-1 message when it reaches the callback and
    aiomqtt exposes no way to defer that), so a failed write is lost — which is why
    the retained `announce`/`presence` state re-syncs on the next reconnect and
    heartbeats repeat every 60 s.
    """
    received_at = now_utc()
    topic = message.topic.value
    payload = message.payload if isinstance(message.payload, bytes | bytearray) else b""
    logger.info(
        "mqtt rx topic=%s qos=%s retain=%s bytes=%d",
        topic,
        message.qos,
        message.retain,
        len(payload),
    )
    try:
        # One session, one transaction, one message. Sequential processing gives
        # per-device ordering for free; at 25 devices on a 60 s heartbeat
        # (`spec/prd.md` → *Capacity*) there is no throughput problem to solve.
        async with sessionmaker() as session:
            event = await handle_up_message(
                session,
                topic=topic,
                payload=bytes(payload),
                retained=message.retain,
                received_at=received_at,
                tolerance=tolerance,
            )
            await session.commit()
        if event is not None:
            logger.info(
                "ingested %s device=%s online=%s", event.type, event.device_id, event.online
            )
    except (SQLAlchemyError, OSError, ValueError) as exc:
        logger.error("ingest failed for topic %s: %s", topic, exc)
    finally:
        touch_heartbeat()


async def run_once(
    hostname: str,
    port: int,
    sessionmaker: async_sessionmaker[AsyncSession],
    tolerance: float,
    username: str | None = None,
    password: str | None = None,
) -> None:
    """Connect, subscribe and consume until the connection drops.

    `username`/`password` are this process's own broker credential (R0-sec-1): a
    read-only dynsec role scoped to `ff/v1/d/+/up/#`, never the API's dynsec admin.
    Both `None` connects anonymously, which the broker refuses — the reconnect loop
    in `run` then repeats "Not authorized" forever, which is the intended loud
    failure. The username is logged; the password never is.
    """
    async with aiomqtt.Client(
        hostname=hostname,
        port=port,
        identifier=CLIENT_ID,
        clean_session=False,
        username=username,
        password=password,
    ) as client:
        logger.info(
            "connected to broker %s:%d as %s (mqtt user %s)",
            hostname,
            port,
            CLIENT_ID,
            username or "<anonymous>",
        )
        await client.subscribe(UP_TOPIC_FILTER, qos=UP_TOPIC_QOS)
        logger.info("subscribed to %s (qos %d)", UP_TOPIC_FILTER, UP_TOPIC_QOS)
        touch_heartbeat()
        async for message in client.messages:
            await handle_message(message, sessionmaker, tolerance)


async def run(
    hostname: str,
    port: int,
    sessionmaker: async_sessionmaker[AsyncSession],
    tolerance: float,
    username: str | None = None,
    password: str | None = None,
) -> None:
    """Consume forever, reconnecting with capped exponential backoff.

    A broker restart must never take the ingestor down with it: an ingestor that
    exits on a dropped connection turns a five-second broker blip into a
    fleet-visibility outage that lasts until someone notices the container.
    """
    delay = RECONNECT_INITIAL_DELAY
    while True:
        try:
            await run_once(hostname, port, sessionmaker, tolerance, username, password)
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
    sessionmaker = get_sessionmaker()

    loop = asyncio.get_running_loop()
    task = asyncio.create_task(
        run(
            settings.mqtt_host,
            settings.mqtt_port,
            sessionmaker,
            settings.presence_tolerance,
            settings.mqtt_username,
            settings.mqtt_password,
        )
    )

    # `docker compose down` sends SIGTERM; cancelling the task lets aiomqtt send a
    # proper DISCONNECT instead of leaving a half-open session on the broker.
    for sig in (signal.SIGTERM, signal.SIGINT):
        with contextlib.suppress(NotImplementedError):
            loop.add_signal_handler(sig, task.cancel)

    with contextlib.suppress(asyncio.CancelledError):
        await task
    # Close the pool rather than leaving connections for the server to reap.
    await get_engine().dispose()
    logger.info("ingestor stopped")


if __name__ == "__main__":
    asyncio.run(main())

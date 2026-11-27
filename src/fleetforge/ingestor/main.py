"""MQTT ingestor — the SOLE MQTT subscriber (`design/production.md`).

Subscribes to `ff/v1/d/+/up/#`, enforces per-device MQTT rate budgets (`A-15`), writes device
and deploy state, emits `ff_events` notifications for SSE fan-out (`R0-be-5`), runs hourly
retention sweeps (`R4-be-1`), and writes structured JSON liveness + capability readiness into
`HEARTBEAT_PATH` (`A-15`).

Run it with `python -m fleetforge.ingestor.main`.
"""

from __future__ import annotations

import asyncio
import contextlib
import json
import logging
import os
import signal
import time
from collections.abc import Mapping
from pathlib import Path
from typing import Any

import aiomqtt
from sqlalchemy.exc import SQLAlchemyError
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from fleetforge import retention
from fleetforge.clock import now_utc
from fleetforge.config import Settings, get_settings
from fleetforge.db.base import get_engine, get_sessionmaker
from fleetforge.ingest import otel
from fleetforge.ingest.handlers import handle_up_message
from fleetforge.ingest.rate_budget import DeviceIngestRateLimiter
from fleetforge.storage.factory import create_object_store, object_store_configured
from fleetforge.storage.objectstore import ObjectStore

logger = logging.getLogger("fleetforge.ingestor")

UP_TOPIC_FILTER = "ff/v1/d/+/up/#"
UP_TOPIC_QOS = 1
CLIENT_ID = "fleetforge-ingestor"
HEARTBEAT_PATH = Path(os.environ.get("INGESTOR_HEARTBEAT_FILE", "/tmp/ingestor-alive"))  # noqa: S108
HEARTBEAT_INTERVAL = 30.0
STALL_LIMIT = 60.0
RECONNECT_INITIAL_DELAY = 1.0
RECONNECT_MAX_DELAY = 30.0


def ingestor_capabilities_status(settings: Settings) -> dict[str, Any]:
    """Return enabled vs disabled ingestor capabilities (`A-15`)."""
    enabled: list[str] = ["mqtt_up_ingest", "retention_sweep"]
    disabled: list[str] = []

    optional = (
        ("object_store_orphan_gc", object_store_configured(settings)),
        (
            "global_otlp_export",
            bool(
                settings.otel_exporter_otlp_endpoint
                and settings.otel_exporter_otlp_endpoint.strip()
            ),
        ),
        (
            "device_rate_budget",
            settings.ingest_device_rate_per_min > 0 and settings.ingest_device_burst > 0,
        ),
    )
    for name, is_enabled in optional:
        if is_enabled:
            enabled.append(name)
        else:
            disabled.append(name)

    return {
        "status": "alive",
        "enabled_capabilities": enabled,
        "disabled_capabilities": disabled,
        "rate_budget": {
            "rate_per_min": settings.ingest_device_rate_per_min,
            "burst": settings.ingest_device_burst,
        },
    }


def log_ingestor_readiness(settings: Settings) -> dict[str, Any]:
    """Log a single structured readiness summary line on startup (`A-15`)."""
    status = ingestor_capabilities_status(settings)
    logger.info(
        "ingestor readiness enabled=%s disabled=%s",
        ",".join(status["enabled_capabilities"]) or "none",
        ",".join(status["disabled_capabilities"]) or "none",
    )
    return status


def touch_heartbeat(status_payload: Mapping[str, Any] | None = None) -> None:
    """Write compact JSON readiness to `HEARTBEAT_PATH` and update its mtime, never raising."""
    payload = dict(status_payload) if status_payload is not None else {"status": "alive"}
    encoded = (json.dumps(payload, separators=(",", ":"), sort_keys=True) + "\n").encode("utf-8")
    now_ts = time.time()
    try:
        tmp_path = HEARTBEAT_PATH.with_name(f".{HEARTBEAT_PATH.name}.{os.getpid()}.tmp")
        tmp_path.write_bytes(encoded)
        os.utime(tmp_path, (now_ts, now_ts))
        os.replace(tmp_path, HEARTBEAT_PATH)
        os.utime(HEARTBEAT_PATH, (now_ts, now_ts))
    except OSError:
        try:
            HEARTBEAT_PATH.write_bytes(encoded)
            os.utime(HEARTBEAT_PATH, (now_ts, now_ts))
        except OSError as exc:  # pragma: no cover - full or read-only /tmp
            logger.warning("heartbeat: cannot touch %s: %s", HEARTBEAT_PATH, exc)


class Consumer:
    """When the message currently being handled started, if any (monotonic clock)."""

    def __init__(self) -> None:
        self.busy_since: float | None = None

    def stalled(self, now: float) -> bool:
        return self.busy_since is not None and now - self.busy_since > STALL_LIMIT


async def keep_alive(
    consumer: Consumer,
    *,
    status_payload: Mapping[str, Any] | None = None,
) -> None:
    """Touch `HEARTBEAT_PATH` every `HEARTBEAT_INTERVAL` until cancelled, unless stalled."""
    while True:
        if consumer.stalled(time.monotonic()):
            logger.warning("heartbeat: one message in flight > %.0fs; not touching", STALL_LIMIT)
        else:
            touch_heartbeat(status_payload)
        await asyncio.sleep(HEARTBEAT_INTERVAL)


async def handle_message(
    message: aiomqtt.Message,
    sessionmaker: async_sessionmaker[AsyncSession],
    tolerance: float,
    *,
    settings: Settings | None = None,
    rate_limiter: DeviceIngestRateLimiter | None = None,
) -> None:
    """Handle one inbound device message; never raise."""
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
        async with sessionmaker() as session:
            event = await handle_up_message(
                session,
                topic=topic,
                payload=bytes(payload),
                retained=message.retain,
                received_at=received_at,
                tolerance=tolerance,
                settings=settings,
                rate_limiter=rate_limiter,
            )
            await session.commit()
        if event is not None:
            logger.info(
                "ingested %s device=%s online=%s", event.type, event.device_id, event.online
            )
    except (SQLAlchemyError, OSError, ValueError) as exc:
        logger.error("ingest failed for topic %s: %s", topic, exc)
    except Exception as exc:  # noqa: BLE001 - one bad message must never kill the sole ingestor
        logger.error(
            "unexpected ingest error for topic %s (%s): %s", topic, type(exc).__name__, exc
        )


async def run_once(
    hostname: str,
    port: int,
    sessionmaker: async_sessionmaker[AsyncSession],
    tolerance: float,
    username: str | None = None,
    password: str | None = None,
    *,
    settings: Settings | None = None,
    rate_limiter: DeviceIngestRateLimiter | None = None,
) -> None:
    """Connect, subscribe, and consume until the connection drops."""
    readiness = ingestor_capabilities_status(settings) if settings is not None else None
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
        consumer = Consumer()
        ticker = asyncio.create_task(keep_alive(consumer, status_payload=readiness))
        try:
            async for message in client.messages:
                consumer.busy_since = time.monotonic()
                try:
                    await handle_message(
                        message,
                        sessionmaker,
                        tolerance,
                        settings=settings,
                        rate_limiter=rate_limiter,
                    )
                finally:
                    consumer.busy_since = None
        finally:
            ticker.cancel()
            with contextlib.suppress(asyncio.CancelledError):
                await ticker


async def run(
    hostname: str,
    port: int,
    sessionmaker: async_sessionmaker[AsyncSession],
    tolerance: float,
    username: str | None = None,
    password: str | None = None,
    *,
    settings: Settings | None = None,
    rate_limiter: DeviceIngestRateLimiter | None = None,
) -> None:
    """Consume forever, reconnecting with capped exponential backoff."""
    limiter = (
        rate_limiter
        if rate_limiter is not None
        else DeviceIngestRateLimiter.from_settings(settings)
    )
    delay = RECONNECT_INITIAL_DELAY
    while True:
        try:
            await run_once(
                hostname,
                port,
                sessionmaker,
                tolerance,
                username,
                password,
                settings=settings,
                rate_limiter=limiter,
            )
            logger.warning("broker connection closed; reconnecting")
        except aiomqtt.MqttError as exc:
            logger.warning("broker error (%s); reconnecting in %.0fs", exc, delay)
        except OSError as exc:
            logger.warning("broker unreachable (%s); reconnecting in %.0fs", exc, delay)
        else:
            delay = RECONNECT_INITIAL_DELAY
        await asyncio.sleep(delay)
        delay = min(delay * 2, RECONNECT_MAX_DELAY)


async def sweep_once(
    sessionmaker: async_sessionmaker[AsyncSession],
    *,
    always_log: bool = False,
    store: ObjectStore | None = None,
) -> retention.SweepResult | None:
    """One retention pass in its own session and transaction; `None` if it failed."""
    try:
        async with sessionmaker() as session:
            if store is None:
                result = await retention.sweep(session, now=now_utc())
                await session.commit()
            else:
                result = await retention.sweep(
                    session, now=now_utc(), store=store, prune_orphans=True
                )
    except (SQLAlchemyError, OSError) as exc:
        logger.error("retention sweep failed: %s", exc)
        return None

    if always_log or result.total:
        extra = ""
        if result.tokens or result.artifact_versions or result.artifacts:
            extra = f" tokens={result.tokens} artifact_versions={result.artifact_versions}" + (
                f" artifacts={result.artifacts}" if result.artifacts else ""
            )
        logger.info(
            "retention sweep deleted logs=%d telemetry_expired=%d telemetry_capped=%d "
            "metrics=%d faults=%d%s",
            result.logs,
            result.telemetry_expired,
            result.telemetry_capped,
            result.metrics,
            result.faults,
            extra,
        )
    return result


async def retention_loop(
    sessionmaker: async_sessionmaker[AsyncSession],
    *,
    interval_s: float = retention.SWEEP_INTERVAL_S,
    store: ObjectStore | None = None,
) -> None:
    """Sweep once at start (always logged), then every `interval_s`, until cancelled."""
    await sweep_once(sessionmaker, always_log=True, store=store)
    while True:
        await asyncio.sleep(interval_s)
        await sweep_once(sessionmaker, store=store)


async def main() -> None:
    """Entrypoint: log readiness, wire SIGTERM/SIGINT to a clean shutdown, then run."""
    logging.basicConfig(
        level=logging.INFO,
        format='{"ts":"%(asctime)s","level":"%(levelname)s","logger":"%(name)s","msg":"%(message)s"}',
        datefmt="%Y-%m-%dT%H:%M:%S%z",
        force=True,
    )
    settings = get_settings()
    sessionmaker = get_sessionmaker()
    readiness = log_ingestor_readiness(settings)
    touch_heartbeat(readiness)
    rate_limiter = DeviceIngestRateLimiter.from_settings(settings)

    loop = asyncio.get_running_loop()
    task = asyncio.create_task(
        run(
            settings.mqtt_host,
            settings.mqtt_port,
            sessionmaker,
            settings.presence_tolerance,
            settings.mqtt_username,
            settings.mqtt_password,
            settings=settings,
            rate_limiter=rate_limiter,
        )
    )

    object_store = create_object_store(settings) if object_store_configured(settings) else None
    sweeper = asyncio.create_task(retention_loop(sessionmaker, store=object_store))
    otel_worker = asyncio.create_task(otel.otel_export_loop())

    for sig in (signal.SIGTERM, signal.SIGINT):
        with contextlib.suppress(NotImplementedError):
            loop.add_signal_handler(sig, task.cancel)

    with contextlib.suppress(asyncio.CancelledError):
        await task
    sweeper.cancel()
    otel_worker.cancel()
    with contextlib.suppress(asyncio.CancelledError):
        await sweeper
    with contextlib.suppress(asyncio.CancelledError):
        await otel_worker
    await otel.flush_otel_queue()
    await otel.close_otel_client()
    await get_engine().dispose()
    logger.info("ingestor stopped")


if __name__ == "__main__":
    asyncio.run(main())

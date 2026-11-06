"""OpenTelemetry OTLP/HTTP JSON exporter for device logs and telemetry (`OTEL-be-1`).

Translates inbound `up/log` batches into OTLP `ExportLogsServiceRequest` (`/v1/logs`)
and `up/telemetry` batches into OTLP `ExportMetricsServiceRequest` (`/v1/metrics`),
enriching each batch with OpenTelemetry resource attributes:
* `service.name`: `<project.slug>`
* `service.version`: `<device.fw_version>`
* `device.id`: `<device.device_id>`
* `fleetforge.project`: `<project.slug>`
* `hardware.platform`: `<device.platform_type>`

Per-project `project.otel_endpoint` (and encrypted `project.otel_headers`) take
precedence over the environment defaults `OTEL_EXPORTER_OTLP_ENDPOINT` and
`OTEL_EXPORTER_OTLP_HEADERS`. Network or collector failures are logged at WARNING
and never interrupt PostgreSQL ingestion.
"""

import asyncio
import dataclasses
import datetime as dt
import json
import logging
import os
import time
import uuid
from typing import Any

import httpx
from pydantic import ValidationError
from sqlalchemy.exc import SQLAlchemyError
from sqlalchemy.ext.asyncio import AsyncSession

from fleetforge import __version__
from fleetforge.clock import device_time
from fleetforge.config import Settings, get_settings
from fleetforge.db.models import Device, Project
from fleetforge.deploys import sanitise_device_text
from fleetforge.ingestor.protocol import LogLine, TelemetrySample
from fleetforge.secrets_crypto import decrypt_secret

logger = logging.getLogger(__name__)

OTEL_HTTP_TIMEOUT_S = 5.0
OTEL_QUEUE_MAX = 256
OTEL_PROJECT_CACHE_TTL_S = 30.0

_ProjectOtelResolved = tuple[str, str, dict[str, str]]
_project_otel_cache: dict[
    tuple[uuid.UUID, str | None, str | None],
    tuple[float, _ProjectOtelResolved | None],
] = {}
_cached_safe_settings: tuple[bool, Settings | None] = (False, None)


def invalidate_project_otel_cache(project_id: uuid.UUID | None = None) -> None:
    """Invalidate cached project OTel endpoint/header resolution."""
    if project_id is None:
        _project_otel_cache.clear()
        return
    stale = [key for key in _project_otel_cache if key[0] == project_id]
    for key in stale:
        _project_otel_cache.pop(key, None)


@dataclasses.dataclass(frozen=True, slots=True)
class OtelExportBatch:
    """Pre-serialized OTLP/HTTP export item queued outside the PostgreSQL transaction."""

    signal: str
    device_id: str
    url: str
    body: dict[str, Any]
    headers: dict[str, str]


_otel_client: httpx.AsyncClient | None = None
_otel_queue: asyncio.Queue[OtelExportBatch] | None = None


def get_otel_client() -> httpx.AsyncClient:
    """Return the shared, connection-pooled `httpx.AsyncClient` for OTLP/HTTP exports."""
    global _otel_client
    if _otel_client is None or _otel_client.is_closed:
        _otel_client = httpx.AsyncClient(
            timeout=OTEL_HTTP_TIMEOUT_S,
            limits=httpx.Limits(max_keepalive_connections=8, max_connections=16),
        )
    return _otel_client


async def close_otel_client() -> None:
    """Close the shared `httpx.AsyncClient` on ingestor shutdown."""
    global _otel_client
    if _otel_client is not None and not _otel_client.is_closed:
        await _otel_client.aclose()
    _otel_client = None


def get_otel_queue() -> asyncio.Queue[OtelExportBatch]:
    """Return the bounded async queue for background OTLP/HTTP exports."""
    global _otel_queue
    if _otel_queue is None:
        _otel_queue = asyncio.Queue(maxsize=OTEL_QUEUE_MAX)
    return _otel_queue


def _enqueue_batch(batch: OtelExportBatch) -> bool:
    """Enqueue an OTLP batch without blocking; drop the oldest entry on backpressure."""
    queue = get_otel_queue()
    if queue.full():
        try:
            dropped = queue.get_nowait()
            queue.task_done()
            logger.warning(
                "otel export queue full (%d); dropped oldest %s batch for device %s",
                OTEL_QUEUE_MAX,
                dropped.signal,
                dropped.device_id,
            )
        except asyncio.QueueEmpty:
            pass
    try:
        queue.put_nowait(batch)
        return True
    except asyncio.QueueFull:
        return False


async def send_otel_batch(batch: OtelExportBatch) -> bool:
    """POST one `OtelExportBatch` using the persistent `httpx.AsyncClient`."""
    client = get_otel_client()
    try:
        resp = await client.post(batch.url, json=batch.body, headers=batch.headers)
        if resp.status_code >= 400:
            logger.warning(
                "otel %s export for device %s returned HTTP %d",
                batch.signal,
                batch.device_id,
                resp.status_code,
            )
            return False
        return True
    except httpx.HTTPError:
        logger.warning(
            "otel %s export failed for device %s",
            batch.signal,
            batch.device_id,
            exc_info=True,
        )
        return False


async def flush_otel_queue() -> int:
    """Drain all currently queued OTLP batches through the persistent HTTP client."""
    queue = get_otel_queue()
    sent = 0
    while not queue.empty():
        try:
            batch = queue.get_nowait()
        except asyncio.QueueEmpty:
            break
        try:
            if await send_otel_batch(batch):
                sent += 1
        finally:
            queue.task_done()
    return sent


async def otel_export_loop() -> None:
    """Background worker that drains queued OTLP batches off the critical DB/MQTT path."""
    queue = get_otel_queue()
    while True:
        batch = await queue.get()
        try:
            await send_otel_batch(batch)
        finally:
            queue.task_done()


_SEVERITY_MAP: dict[str, tuple[int, str]] = {
    "E": (17, "ERROR"),
    "ERR": (17, "ERROR"),
    "ERROR": (17, "ERROR"),
    "W": (13, "WARN"),
    "WARN": (13, "WARN"),
    "WARNING": (13, "WARN"),
    "I": (9, "INFO"),
    "INFO": (9, "INFO"),
    "D": (5, "DEBUG"),
    "DEBUG": (5, "DEBUG"),
}


def parse_otel_headers(raw: str | None) -> dict[str, str]:
    """Parse OTLP headers from JSON object, newline/comma `Key: Value`, or `Key=Value` format."""
    if not raw or not raw.strip():
        return {}
    text = raw.strip()
    if text.startswith("{"):
        try:
            parsed = json.loads(text)
            if isinstance(parsed, dict):
                return {str(k).strip(): str(v).strip() for k, v in parsed.items() if str(k).strip()}
        except json.JSONDecodeError:
            pass
    headers: dict[str, str] = {}
    # Split on newlines first; if single-line, split on commas
    parts = text.splitlines() if "\n" in text else text.split(",")
    for part in parts:
        item = part.strip()
        if not item:
            continue
        if ":" in item and ("=" not in item or item.index(":") < item.index("=")):
            k, _, v = item.partition(":")
        elif "=" in item:
            k, _, v = item.partition("=")
        else:
            continue
        if k.strip():
            headers[k.strip()] = v.strip()
    return headers


def _resolve_endpoint(base_url: str, signal_suffix: str) -> str:
    """Append `/v1/logs` or `/v1/metrics` unless `base_url` already ends with it."""
    stripped = base_url.strip().rstrip("/")
    if stripped.endswith(signal_suffix):
        return stripped
    for known in ("/v1/logs", "/v1/metrics", "/v1/traces"):
        if stripped.endswith(known):
            stripped = stripped[: -len(known)]
            break
    return f"{stripped}{signal_suffix}"


def _attr(key: str, value: Any) -> dict[str, Any] | None:
    """One OTLP KeyValue, typed from the Python value; None for None/empty."""
    if value is None:
        return None
    if isinstance(value, bool):
        return {"key": key, "value": {"boolValue": value}}
    if isinstance(value, int):
        return {"key": key, "value": {"intValue": str(value)}}
    if isinstance(value, float):
        return {"key": key, "value": {"doubleValue": value}}
    text = str(value)
    if not text:
        return None
    return {"key": key, "value": {"stringValue": text}}


def _resource_attributes(device: Device, project_slug: str) -> list[dict[str, Any]]:
    """The board's identity, once per batch (OTel resource): what the announce said.

    Everything here is already in the `devices` row, so a firmware line never has to
    repeat it. Semantic-convention names where one exists (`service.*`, `host.arch`,
    `device.*`), `firmware.*` / `fleetforge.*` for the rest.
    """
    candidates: list[tuple[str, Any]] = [
        ("service.name", project_slug),
        ("service.version", device.fw_version),
        ("service.instance.id", device.device_id),
        ("fleetforge.project", project_slug),
        ("device.id", device.device_id),
        ("device.name", device.name),
        ("hardware.platform", device.platform_type),
        ("host.arch", device.platform_type),
        ("firmware.version", device.fw_version),
        ("firmware.library.version", device.agent_version),
        ("firmware.partition_layout", device.partition_layout),
        ("firmware.boot_ok", device.boot_ok),
        ("firmware.rollback_capable", device.rollback_capable),
        ("device.flash_chip_size", device.flash_chip_size),
        ("device.link_type", device.link_type),
        ("device.power_class", device.power_class),
        ("device.reset_reason", device.last_reset_reason),
        ("device.is_dev", device.is_dev),
        ("device.group_id", str(device.group_id) if device.group_id else None),
        ("fleetforge.secrets_rev", device.secrets_rev),
    ]
    for name, version in sorted((device.sub_versions or {}).items()):
        candidates.append((f"firmware.sub_versions.{name}", version))
    attrs = [a for key, value in candidates if (a := _attr(key, value)) is not None]
    return attrs


def device_snapshot(device: Device | None) -> dict[str, Any]:
    """What was true on the board around the time of a line: the latest heartbeat.

    Stored on each `device_logs` row as `context` and attached to each OTLP record, so
    "heap was 41 KB when this W fired" survives the next heartbeat overwriting the row.
    Keys are the OTel attribute names; values are plain JSON scalars.
    """
    if device is None:
        return {}
    candidates: list[tuple[str, Any]] = [
        ("firmware.version", device.fw_version),
        ("device.rssi_dbm", device.rssi),
        ("device.free_heap_bytes", device.free_heap),
        ("device.min_free_heap_bytes", device.min_free_heap),
        ("device.uptime_s", device.uptime_s),
        ("fleetforge.secrets_rev", device.secrets_rev),
        ("fleetforge.health_at", device.health_at.isoformat() if device.health_at else None),
    ]
    return {key: value for key, value in candidates if value is not None and value != ""}


def build_otlp_logs_payload(
    *,
    device: Device,
    project_slug: str,
    lines: list[LogLine],
    dropped: int,
    received_at: dt.datetime,
) -> dict[str, Any]:
    """Construct an OTLP/HTTP JSON `ExportLogsServiceRequest` payload."""
    observed_ns = str(int(received_at.timestamp() * 1_000_000_000))
    snapshot_attrs = [
        a for key, value in device_snapshot(device).items() if (a := _attr(key, value)) is not None
    ]
    log_records: list[dict[str, Any]] = []
    for idx, line in enumerate(lines):
        logged_at = device_time(received_at, line.age_ms)
        time_ns = str(int(logged_at.timestamp() * 1_000_000_000))
        sev_num, sev_text = _SEVERITY_MAP.get(line.level.upper(), (9, line.level))
        clean_text = sanitise_device_text(line.text) or ""
        attrs: list[dict[str, Any]] = [
            {"key": "fleetforge.age_ms", "value": {"intValue": str(line.age_ms)}},
            *snapshot_attrs,
        ]
        if idx == 0 and dropped > 0:
            attrs.append({"key": "fleetforge.dropped_before", "value": {"intValue": str(dropped)}})
        log_records.append(
            {
                "timeUnixNano": time_ns,
                "observedTimeUnixNano": observed_ns,
                "severityNumber": sev_num,
                "severityText": sev_text,
                "body": {"stringValue": clean_text},
                "attributes": attrs,
            }
        )

    return {
        "resourceLogs": [
            {
                "resource": {"attributes": _resource_attributes(device, project_slug)},
                "scopeLogs": [
                    {
                        "scope": {"name": "fleetforge.ingestor", "version": __version__},
                        "logRecords": log_records,
                    }
                ],
            }
        ]
    }


def build_otlp_metrics_payload(
    *,
    device: Device,
    project_slug: str,
    samples: list[TelemetrySample],
    received_at: dt.datetime,
) -> dict[str, Any]:
    """Construct an OTLP/HTTP JSON `ExportMetricsServiceRequest` payload."""
    by_name: dict[str, list[dict[str, Any]]] = {}
    for sample in samples:
        sampled_at = device_time(received_at, sample.age_ms)
        time_ns = str(int(sampled_at.timestamp() * 1_000_000_000))
        by_name.setdefault(sample.name, []).append(
            {
                "timeUnixNano": time_ns,
                "asDouble": float(sample.value),
            }
        )

    metrics = [
        {
            "name": name,
            "gauge": {"dataPoints": points},
        }
        for name, points in by_name.items()
    ]

    return {
        "resourceMetrics": [
            {
                "resource": {"attributes": _resource_attributes(device, project_slug)},
                "scopeMetrics": [
                    {
                        "scope": {"name": "fleetforge.ingestor", "version": __version__},
                        "metrics": metrics,
                    }
                ],
            }
        ]
    }


def _safe_settings() -> Settings | None:
    global _cached_safe_settings
    if get_settings.cache_info().currsize > 0:
        return get_settings()
    initialized, cached = _cached_safe_settings
    if initialized:
        return cached
    try:
        resolved = get_settings()
    except ValidationError:
        resolved = None
    _cached_safe_settings = (True, resolved)
    return resolved


async def _resolve_project_otel(
    session: AsyncSession, device: Device
) -> tuple[str, str, dict[str, str]] | None:
    """Return `(project_slug, base_endpoint, headers)` if OTel export is enabled, else `None`.

    Caches the resolved project OTel configuration (and decrypted headers) in memory for
    `OTEL_PROJECT_CACHE_TTL_S` seconds so high-frequency `up/log` and `up/telemetry` packets
    do not open a DB SAVEPOINT, query `projects`, or run AES-GCM decryption on every message.
    """
    settings = _safe_settings()
    env_endpoint = (
        settings.otel_exporter_otlp_endpoint
        if settings and settings.otel_exporter_otlp_endpoint
        else os.environ.get("OTEL_EXPORTER_OTLP_ENDPOINT")
    )
    env_headers = (
        settings.otel_exporter_otlp_headers
        if settings and settings.otel_exporter_otlp_headers
        else os.environ.get("OTEL_EXPORTER_OTLP_HEADERS")
    )

    project_id = device.project_id
    cache_key = (project_id, env_endpoint, env_headers)
    now_mono = time.monotonic()

    identity_map = getattr(session, "identity_map", None)
    in_identity_map = bool(
        identity_map is not None
        and hasattr(identity_map, "values")
        and any(isinstance(obj, Project) and obj.id == project_id for obj in identity_map.values())
    )
    if not in_identity_map and cache_key in _project_otel_cache:
        expires_at, cached_result = _project_otel_cache[cache_key]
        if now_mono < expires_at:
            return cached_result

    try:
        async with session.begin_nested():
            project = await session.get(Project, project_id)
    except SQLAlchemyError:
        logger.warning(
            "device %s: project OTel lookup failed; skipping OTel export",
            device.device_id,
            exc_info=True,
        )
        return None

    project_slug = project.slug if project is not None else "default"

    endpoint: str | None = None
    raw_headers: str | None = None

    if project is not None and project.otel_endpoint:
        endpoint = project.otel_endpoint
        if project.otel_headers:
            try:
                raw_headers = decrypt_secret(project.otel_headers, settings)
            except ValueError:
                raw_headers = project.otel_headers
    else:
        endpoint = env_endpoint
        raw_headers = env_headers

    result: _ProjectOtelResolved | None = (
        (project_slug, endpoint, parse_otel_headers(raw_headers)) if endpoint else None
    )
    if not in_identity_map:
        _project_otel_cache[cache_key] = (now_mono + OTEL_PROJECT_CACHE_TTL_S, result)
    return result


async def _build_logs_batch(
    session: AsyncSession,
    *,
    device: Device,
    lines: list[LogLine],
    dropped: int,
    received_at: dt.datetime,
) -> OtelExportBatch | None:
    if not lines:
        return None
    resolved = await _resolve_project_otel(session, device)
    if resolved is None:
        return None
    project_slug, base_endpoint, headers = resolved
    url = _resolve_endpoint(base_endpoint, "/v1/logs")
    body = build_otlp_logs_payload(
        device=device,
        project_slug=project_slug,
        lines=lines,
        dropped=dropped,
        received_at=received_at,
    )
    return OtelExportBatch(
        signal="logs",
        device_id=device.device_id,
        url=url,
        body=body,
        headers={"Content-Type": "application/json", **headers},
    )


async def _build_metrics_batch(
    session: AsyncSession,
    *,
    device: Device,
    samples: list[TelemetrySample],
    received_at: dt.datetime,
) -> OtelExportBatch | None:
    if not samples:
        return None
    resolved = await _resolve_project_otel(session, device)
    if resolved is None:
        return None
    project_slug, base_endpoint, headers = resolved
    url = _resolve_endpoint(base_endpoint, "/v1/metrics")
    body = build_otlp_metrics_payload(
        device=device,
        project_slug=project_slug,
        samples=samples,
        received_at=received_at,
    )
    return OtelExportBatch(
        signal="metrics",
        device_id=device.device_id,
        url=url,
        body=body,
        headers={"Content-Type": "application/json", **headers},
    )


async def enqueue_logs_to_otel(
    session: AsyncSession,
    *,
    device: Device,
    lines: list[LogLine],
    dropped: int,
    received_at: dt.datetime,
) -> bool:
    """Resolve OTel config in `session` and enqueue the `/v1/logs` batch without blocking."""
    batch = await _build_logs_batch(
        session,
        device=device,
        lines=lines,
        dropped=dropped,
        received_at=received_at,
    )
    if batch is None:
        return False
    return _enqueue_batch(batch)


async def enqueue_metrics_to_otel(
    session: AsyncSession,
    *,
    device: Device,
    samples: list[TelemetrySample],
    received_at: dt.datetime,
) -> bool:
    """Resolve OTel config in `session` and enqueue the `/v1/metrics` batch without blocking."""
    batch = await _build_metrics_batch(
        session,
        device=device,
        samples=samples,
        received_at=received_at,
    )
    if batch is None:
        return False
    return _enqueue_batch(batch)


async def export_logs_to_otel(
    session: AsyncSession,
    *,
    device: Device,
    lines: list[LogLine],
    dropped: int,
    received_at: dt.datetime,
) -> bool:
    """Export a device log batch to OTLP/HTTP `/v1/logs` via the shared `httpx.AsyncClient`."""
    batch = await _build_logs_batch(
        session,
        device=device,
        lines=lines,
        dropped=dropped,
        received_at=received_at,
    )
    if batch is None:
        return False
    return await send_otel_batch(batch)


async def export_metrics_to_otel(
    session: AsyncSession,
    *,
    device: Device,
    samples: list[TelemetrySample],
    received_at: dt.datetime,
) -> bool:
    """Export a device telemetry batch to OTLP/HTTP `/v1/metrics` via the shared `httpx.AsyncClient`."""
    batch = await _build_metrics_batch(
        session,
        device=device,
        samples=samples,
        received_at=received_at,
    )
    if batch is None:
        return False
    return await send_otel_batch(batch)

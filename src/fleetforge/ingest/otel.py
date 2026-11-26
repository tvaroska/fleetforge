"""OpenTelemetry OTLP/HTTP JSON exporter for device logs and telemetry (`OTEL-be-1`, `A-29`, `A-32`).

Per-project `project.otel_endpoint` (and encrypted `project.otel_headers`) take precedence
over `OTEL_EXPORTER_OTLP_ENDPOINT` / `OTEL_EXPORTER_OTLP_HEADERS`. The in-process cache is
keyed on `(project_id, project.updated_at, env_endpoint, env_headers)` so an update
committed by `fleetforge-api` invalidates `fleetforge-ingestor`'s cache immediately without
cross-process dictionary mutation.
"""

import asyncio
import dataclasses
import datetime as dt
import hashlib
import json
import logging
import os
import time
import uuid
from typing import Any

import httpx
from sqlalchemy import select
from sqlalchemy.exc import SQLAlchemyError
from sqlalchemy.ext.asyncio import AsyncSession

from fleetforge import __version__
from fleetforge.clock import device_time
from fleetforge.config import Settings
from fleetforge.db.models import Device, Project
from fleetforge.deploys import sanitise_device_text
from fleetforge.ingest.protocol import LogLine, TelemetrySample
from fleetforge.secrets_crypto import decrypt_secret
from fleetforge.url_safety import (
    public_https_transport,
    resolve_and_validate_public_https_url,
    validate_public_https_url,
)

logger = logging.getLogger(__name__)

OTEL_HTTP_TIMEOUT_S = 5.0
OTEL_QUEUE_MAX = 256
OTEL_PROJECT_CACHE_TTL_S = 30.0

_ProjectOtelResolved = tuple[str, str, dict[str, str], bool]
_project_otel_cache: dict[
    tuple[uuid.UUID, str | None, str | None, str | None, str],
    tuple[float, _ProjectOtelResolved | None],
] = {}


def invalidate_project_otel_cache(project_id: uuid.UUID | None = None) -> None:
    """Clear cached project OTel endpoint/header resolution (primarily for tests)."""
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
    require_public_https: bool = True


_otel_client: httpx.AsyncClient | None = None
_env_otel_client: httpx.AsyncClient | None = None
_otel_queue: asyncio.Queue[OtelExportBatch] | None = None


def get_otel_client() -> httpx.AsyncClient:
    """Return the shared, connection-pooled public-only `httpx.AsyncClient` for OTLP/HTTP exports."""
    global _otel_client
    if _otel_client is None or _otel_client.is_closed:
        limits = httpx.Limits(max_keepalive_connections=8, max_connections=16)
        _otel_client = httpx.AsyncClient(
            timeout=OTEL_HTTP_TIMEOUT_S,
            follow_redirects=False,
            trust_env=False,
            transport=public_https_transport(limits=limits, field_name="otel_endpoint"),
        )
    return _otel_client


def _get_env_otel_client() -> httpx.AsyncClient:
    """Return the shared `httpx.AsyncClient` for operator-configured env OTLP collectors."""
    global _env_otel_client
    if _env_otel_client is None or _env_otel_client.is_closed:
        _env_otel_client = httpx.AsyncClient(
            timeout=OTEL_HTTP_TIMEOUT_S,
            follow_redirects=False,
            trust_env=False,
            limits=httpx.Limits(max_keepalive_connections=8, max_connections=16),
        )
    return _env_otel_client


async def close_otel_client() -> None:
    """Close the shared `httpx.AsyncClient` instances on ingestor shutdown."""
    global _otel_client, _env_otel_client
    if _otel_client is not None and not _otel_client.is_closed:
        await _otel_client.aclose()
    _otel_client = None
    if _env_otel_client is not None and not _env_otel_client.is_closed:
        await _env_otel_client.aclose()
    _env_otel_client = None


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
    target_url = batch.url
    if batch.require_public_https:
        try:
            target_url = await resolve_and_validate_public_https_url(
                batch.url, field_name="otel_endpoint"
            )
        except ValueError as exc:
            logger.warning(
                "otel %s export blocked for device %s: %s",
                batch.signal,
                batch.device_id,
                exc,
            )
            return False
    client = get_otel_client() if batch.require_public_https else _get_env_otel_client()
    try:
        resp = await client.post(target_url, json=batch.body, headers=batch.headers)
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
    """The board's identity, once per batch (OTel resource): what the announce said."""
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
    return [a for key, value in candidates if (a := _attr(key, value)) is not None]


def device_snapshot(device: Device | None) -> dict[str, Any]:
    """What was true on the board around the time of a line: the latest heartbeat."""
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

    metrics = [{"name": name, "gauge": {"dataPoints": points}} for name, points in by_name.items()]

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


def _settings_key_fingerprint(cfg: Settings | None) -> str:
    if cfg is None:
        return ""
    raw = f"{cfg.secrets_encryption_key or ''}\x00{cfg.artifact_url_secret or ''}"
    return hashlib.sha256(raw.encode("utf-8")).hexdigest()[:16]


async def _resolve_project_otel_with_source(
    session: AsyncSession,
    device: Device,
    *,
    settings: Settings | None = None,
) -> _ProjectOtelResolved | None:
    """Return `(project_slug, base_endpoint, headers, is_project_configured)` if enabled."""
    cfg = settings
    env_endpoint = (
        cfg.otel_exporter_otlp_endpoint
        if cfg and cfg.otel_exporter_otlp_endpoint
        else os.environ.get("OTEL_EXPORTER_OTLP_ENDPOINT")
    )
    env_headers = (
        cfg.otel_exporter_otlp_headers
        if cfg and cfg.otel_exporter_otlp_headers
        else os.environ.get("OTEL_EXPORTER_OTLP_HEADERS")
    )
    key_fp = _settings_key_fingerprint(cfg)

    project_id = device.project_id
    now_mono = time.monotonic()

    updated_key: str | None = None
    if isinstance(session, AsyncSession):
        try:
            async with session.begin_nested():
                updated_val = await session.scalar(
                    select(Project.updated_at).where(Project.id == project_id)
                )
            updated_key = updated_val.isoformat() if updated_val is not None else None
        except SQLAlchemyError:
            logger.warning(
                "device %s: project OTel version check failed; skipping OTel export",
                device.device_id,
                exc_info=True,
            )
            return None
    elif callable(getattr(session, "scalar", None)):
        raw_updated = await session.scalar(
            select(Project.updated_at).where(Project.id == project_id)
        )
        if isinstance(raw_updated, dt.datetime):
            updated_key = raw_updated.isoformat()

    cache_key = (project_id, updated_key, env_endpoint, env_headers, key_fp)
    if cache_key in _project_otel_cache:
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

    if updated_key is None and project is not None:
        proj_updated = getattr(project, "updated_at", None)
        if isinstance(proj_updated, dt.datetime):
            updated_key = proj_updated.isoformat()
            cache_key = (project_id, updated_key, env_endpoint, env_headers, key_fp)

    project_slug = project.slug if project is not None else "default"
    endpoint: str | None = None
    raw_headers: str | None = None
    is_project_configured = False

    if project is not None and project.otel_endpoint:
        try:
            endpoint = validate_public_https_url(project.otel_endpoint, field_name="otel_endpoint")
            if project.otel_headers:
                raw_headers = decrypt_secret(project.otel_headers, cfg)
            is_project_configured = True
        except ValueError as exc:
            logger.error(
                "device %s: invalid or undecryptable project OTel config (%s); skipping OTel export",
                device.device_id,
                exc,
            )
            return None
    else:
        endpoint = env_endpoint
        raw_headers = env_headers

    result: _ProjectOtelResolved | None = (
        (project_slug, endpoint, parse_otel_headers(raw_headers), is_project_configured)
        if endpoint
        else None
    )
    invalidate_project_otel_cache(project_id)
    _project_otel_cache[cache_key] = (now_mono + OTEL_PROJECT_CACHE_TTL_S, result)
    return result


async def _resolve_project_otel(
    session: AsyncSession,
    device: Device,
    *,
    settings: Settings | None = None,
) -> tuple[str, str, dict[str, str]] | None:
    """Return `(project_slug, base_endpoint, headers)` if OTel export is enabled, else `None`."""
    resolved = await _resolve_project_otel_with_source(session, device, settings=settings)
    if resolved is None:
        return None
    project_slug, endpoint, headers, _ = resolved
    return project_slug, endpoint, headers


async def _build_logs_batch(
    session: AsyncSession,
    *,
    device: Device,
    lines: list[LogLine],
    dropped: int,
    received_at: dt.datetime,
    settings: Settings | None = None,
) -> OtelExportBatch | None:
    if not lines:
        return None
    resolved = await _resolve_project_otel_with_source(session, device, settings=settings)
    if resolved is None:
        return None
    project_slug, base_endpoint, headers, is_project_configured = resolved
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
        require_public_https=is_project_configured,
    )


async def _build_metrics_batch(
    session: AsyncSession,
    *,
    device: Device,
    samples: list[TelemetrySample],
    received_at: dt.datetime,
    settings: Settings | None = None,
) -> OtelExportBatch | None:
    if not samples:
        return None
    resolved = await _resolve_project_otel_with_source(session, device, settings=settings)
    if resolved is None:
        return None
    project_slug, base_endpoint, headers, is_project_configured = resolved
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
        require_public_https=is_project_configured,
    )


async def enqueue_logs_to_otel(
    session: AsyncSession,
    *,
    device: Device,
    lines: list[LogLine],
    dropped: int,
    received_at: dt.datetime,
    settings: Settings | None = None,
) -> bool:
    """Resolve OTel config in `session` and enqueue the `/v1/logs` batch without blocking."""
    batch = await _build_logs_batch(
        session,
        device=device,
        lines=lines,
        dropped=dropped,
        received_at=received_at,
        settings=settings,
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
    settings: Settings | None = None,
) -> bool:
    """Resolve OTel config in `session` and enqueue the `/v1/metrics` batch without blocking."""
    batch = await _build_metrics_batch(
        session,
        device=device,
        samples=samples,
        received_at=received_at,
        settings=settings,
    )
    if batch is None:
        return False
    return _enqueue_batch(batch)

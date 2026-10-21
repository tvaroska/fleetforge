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

import datetime as dt
import json
import logging
import os
from typing import Any

import httpx
from pydantic import ValidationError
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


def _resource_attributes(device: Device, project_slug: str) -> list[dict[str, Any]]:
    attrs: list[dict[str, Any]] = [
        {"key": "service.name", "value": {"stringValue": project_slug}},
        {"key": "fleetforge.project", "value": {"stringValue": project_slug}},
        {"key": "device.id", "value": {"stringValue": device.device_id}},
        {"key": "hardware.platform", "value": {"stringValue": device.platform_type}},
    ]
    if device.fw_version:
        attrs.append({"key": "service.version", "value": {"stringValue": device.fw_version}})
        attrs.append({"key": "firmware.version", "value": {"stringValue": device.fw_version}})
    if device.name:
        attrs.append({"key": "device.name", "value": {"stringValue": device.name}})
    return attrs


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
    log_records: list[dict[str, Any]] = []
    for idx, line in enumerate(lines):
        logged_at = device_time(received_at, line.age_ms)
        time_ns = str(int(logged_at.timestamp() * 1_000_000_000))
        sev_num, sev_text = _SEVERITY_MAP.get(line.level.upper(), (9, line.level))
        clean_text = sanitise_device_text(line.text) or ""
        attrs: list[dict[str, Any]] = [
            {"key": "fleetforge.age_ms", "value": {"intValue": str(line.age_ms)}},
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
    try:
        return get_settings()
    except ValidationError:
        return None


async def _resolve_project_otel(
    session: AsyncSession, device: Device
) -> tuple[str, str, dict[str, str]] | None:
    """Return `(project_slug, base_endpoint, headers)` if OTel export is enabled, else `None`."""
    project = await session.get(Project, device.project_id)
    project_slug = project.slug if project is not None else "default"
    settings = _safe_settings()

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
        endpoint = (
            settings.otel_exporter_otlp_endpoint
            if settings and settings.otel_exporter_otlp_endpoint
            else os.environ.get("OTEL_EXPORTER_OTLP_ENDPOINT")
        )
        raw_headers = (
            settings.otel_exporter_otlp_headers
            if settings and settings.otel_exporter_otlp_headers
            else os.environ.get("OTEL_EXPORTER_OTLP_HEADERS")
        )

    if not endpoint:
        return None

    return project_slug, endpoint, parse_otel_headers(raw_headers)


async def export_logs_to_otel(
    session: AsyncSession,
    *,
    device: Device,
    lines: list[LogLine],
    dropped: int,
    received_at: dt.datetime,
) -> bool:
    """Export a device log batch to OTLP/HTTP `/v1/logs` if configured."""
    if not lines:
        return False
    resolved = await _resolve_project_otel(session, device)
    if resolved is None:
        return False
    project_slug, base_endpoint, headers = resolved
    url = _resolve_endpoint(base_endpoint, "/v1/logs")
    body = build_otlp_logs_payload(
        device=device,
        project_slug=project_slug,
        lines=lines,
        dropped=dropped,
        received_at=received_at,
    )
    req_headers = {"Content-Type": "application/json", **headers}
    try:
        async with httpx.AsyncClient(timeout=OTEL_HTTP_TIMEOUT_S) as client:
            resp = await client.post(url, json=body, headers=req_headers)
            if resp.status_code >= 400:
                logger.warning(
                    "otel logs export for device %s returned HTTP %d",
                    device.device_id,
                    resp.status_code,
                )
                return False
        return True
    except httpx.HTTPError:
        logger.warning("otel logs export failed for device %s", device.device_id, exc_info=True)
        return False


async def export_metrics_to_otel(
    session: AsyncSession,
    *,
    device: Device,
    samples: list[TelemetrySample],
    received_at: dt.datetime,
) -> bool:
    """Export a device telemetry batch to OTLP/HTTP `/v1/metrics` if configured."""
    if not samples:
        return False
    resolved = await _resolve_project_otel(session, device)
    if resolved is None:
        return False
    project_slug, base_endpoint, headers = resolved
    url = _resolve_endpoint(base_endpoint, "/v1/metrics")
    body = build_otlp_metrics_payload(
        device=device,
        project_slug=project_slug,
        samples=samples,
        received_at=received_at,
    )
    req_headers = {"Content-Type": "application/json", **headers}
    try:
        async with httpx.AsyncClient(timeout=OTEL_HTTP_TIMEOUT_S) as client:
            resp = await client.post(url, json=body, headers=req_headers)
            if resp.status_code >= 400:
                logger.warning(
                    "otel metrics export for device %s returned HTTP %d",
                    device.device_id,
                    resp.status_code,
                )
                return False
        return True
    except httpx.HTTPError:
        logger.warning("otel metrics export failed for device %s", device.device_id, exc_info=True)
        return False

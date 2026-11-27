"""Shared ingestor test helpers and spec example extractors (`T-11`)."""

from __future__ import annotations

import datetime as dt
import json
import re
from pathlib import Path
from typing import Any

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from fleetforge.db.models import Device
from fleetforge.events import DeviceEvent
from fleetforge.ingest.rate_budget import DeviceIngestRateLimiter
from fleetforge.ingestor.handlers import handle_up_message

REPO_ROOT = Path(__file__).resolve().parent.parent
DEVICE_PROTOCOL_MD = REPO_ROOT / "spec" / "device-protocol.md"
HEALTH_TELEMETRY_DOC = REPO_ROOT / "docs" / "features" / "health-telemetry.md"

HB_SPEC_HEADING = "### `up/hb` — heartbeat"
TELEMETRY_SPEC_HEADING = "### `up/telemetry` — user-defined metrics"

DEVICE_ID = "a4cf12b3de90"
NOW = dt.datetime(2026, 9, 8, 12, 0, tzinfo=dt.UTC)
TOLERANCE = 2.5

ANNOUNCE: dict[str, Any] = {
    "proto": 1,
    "device_id": DEVICE_ID,
    "platform_type": "esp32c6",
    "fw_version": "1.4.2",
    "agent_version": "0.3.0",
    "link_type": "wifi",
    "power_class": "always_on",
    "partition_layout": "ab-4m-v1",
    "ota_slot_size": 1966080,
    "capabilities": ["ota", "selftest"],
}


async def seed_device(
    session: AsyncSession, device_id: str = DEVICE_ID, **overrides: object
) -> Device:
    values: dict[str, object] = {
        "device_id": device_id,
        "platform_type": "esp32c6",
        "link_type": "wifi",
        "power_class": "always_on",
    }
    values.update(overrides)
    device = Device(**values)
    session.add(device)
    await session.flush()
    return device


async def publish(
    session: AsyncSession,
    channel: str,
    body: object = None,
    *,
    device_id: str = DEVICE_ID,
    retained: bool = False,
    at: dt.datetime = NOW,
    raw: bytes | None = None,
    topic: str | None = None,
    rate_limiter: DeviceIngestRateLimiter | None = None,
) -> DeviceEvent | None:
    """Drive one `up/` message through `handle_up_message` with standard defaults."""
    payload = raw if raw is not None else json.dumps(body).encode()
    return await handle_up_message(
        session,
        topic=topic if topic is not None else f"ff/v1/d/{device_id}/up/{channel}",
        payload=payload,
        retained=retained,
        received_at=at,
        tolerance=TOLERANCE,
        rate_limiter=rate_limiter,
    )


async def reload(session: AsyncSession, device_id: str = DEVICE_ID) -> Device:
    device = await session.scalar(select(Device).where(Device.device_id == device_id))
    assert device is not None
    await session.refresh(device)
    return device


def json_fence_after(markdown: str, heading: str) -> str:
    """Return the first ```json fenced block appearing after `heading` in `markdown`."""
    idx = markdown.find(heading)
    if idx < 0:
        raise ValueError(f"heading {heading!r} not found")
    match = re.search(r"```json\s*\n(.*?)\n```", markdown[idx:], re.DOTALL)
    if match is None:
        raise ValueError(f"no json fence found after {heading!r}")
    return match.group(1)


def spec_hb_example() -> dict[str, Any]:
    """Load the canonical `up/hb` JSON example from `spec/device-protocol.md`."""
    raw = json_fence_after(DEVICE_PROTOCOL_MD.read_text(encoding="utf-8"), HB_SPEC_HEADING)
    parsed: dict[str, Any] = json.loads(raw)
    return parsed


def telemetry_example() -> str:
    """Load the canonical `up/telemetry` JSON example from the spec or feature doc."""
    proto_text = DEVICE_PROTOCOL_MD.read_text(encoding="utf-8")
    if TELEMETRY_SPEC_HEADING in proto_text:
        return json_fence_after(proto_text, TELEMETRY_SPEC_HEADING)
    return json_fence_after(
        HEALTH_TELEMETRY_DOC.read_text(encoding="utf-8"), TELEMETRY_SPEC_HEADING
    )

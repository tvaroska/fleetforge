"""UGV edge-relay gateway resolution & delta OTA lookup (`V3-1`, `V3-2`).

Provides:
- `validate_relay_base_url`: validates a UGV gateway's local edge HTTP/HTTPS cache origin.
- `resolve_parent_relay_url`: resolves `device.parent_device_id` -> `GatewayRelayState.relay_base_url`
  within the same `project_id` so child drones fetch staged artifacts through their parent UGV.
- `resolve_delta_for_device`: resolves a precomputed `ArtifactDelta` (`ffdelta-v1`) when the
  target board announces `"delta"` in `capabilities` and runs a known `from_version`.
"""

from __future__ import annotations

import urllib.parse
import uuid
from collections.abc import Callable
from typing import Any

from sqlalchemy import select, text
from sqlalchemy.ext.asyncio import AsyncSession

from fleetforge.config import Settings
from fleetforge.db.models import ArtifactDelta, Device, GatewayRelayState

_FROM_VERSION_SHA_SQL = text(
    """
    SELECT sha256
      FROM artifact_versions
     WHERE project_id = :project_id AND target = :target AND version = :version
    """
)


def validate_relay_base_url(raw: str) -> str:
    """Validate an HTTP/HTTPS UGV edge-relay origin (`http://192.168.4.1:8080`, etc.)."""
    cleaned = raw.strip().rstrip("/")
    if not cleaned or any(ord(ch) < 33 or ord(ch) == 127 for ch in cleaned):
        raise ValueError("relay_base_url must be a valid http:// or https:// origin")
    parsed = urllib.parse.urlsplit(cleaned)
    if parsed.scheme not in ("http", "https") or not parsed.hostname:
        raise ValueError("relay_base_url must use http:// or https:// and include a host")
    if parsed.username or parsed.password or parsed.query or parsed.fragment:
        raise ValueError("relay_base_url must not include credentials, query string, or fragment")
    try:
        _ = parsed.port
    except ValueError as exc:
        raise ValueError("relay_base_url has an invalid port") from exc
    return cleaned


async def resolve_parent_relay_url(
    session: AsyncSession,
    *,
    device: Device,
    needed_sha256: str,
) -> str | None:
    """Return the parent UGV gateway's `relay_base_url` when `device` routes via a live parent."""
    parent_id = device.parent_device_id
    if not parent_id:
        return None
    parent = await session.get(Device, parent_id)
    if (
        not isinstance(parent, Device)
        or parent.decommissioned_at is not None
        or parent.project_id != device.project_id
    ):
        return None
    relay = await session.get(GatewayRelayState, parent_id)
    if not isinstance(relay, GatewayRelayState):
        return None
    if (
        needed_sha256 in relay.cached_artifacts
        or "edge_relay" in (parent.capabilities or [])
        or len(relay.cached_artifacts) == 0
    ):
        return relay.relay_base_url
    return None


async def resolve_delta_for_device(
    session: AsyncSession,
    *,
    device: Device,
    project_id: uuid.UUID,
    to_sha256: str,
) -> ArtifactDelta | None:
    """Return a smaller `ArtifactDelta` when `device` announces `'delta'` and runs `from_version`."""
    if "delta" not in (device.capabilities or []) or not device.fw_version:
        return None
    from_sha = await session.scalar(
        _FROM_VERSION_SHA_SQL,
        {
            "project_id": project_id,
            "target": device.platform_type,
            "version": device.fw_version,
        },
    )
    if not isinstance(from_sha, str) or from_sha == to_sha256:
        return None
    delta = await session.scalar(
        select(ArtifactDelta).where(
            ArtifactDelta.from_sha256 == from_sha,
            ArtifactDelta.to_sha256 == to_sha256,
        )
    )
    if not isinstance(delta, ArtifactDelta) or delta.patch_size_bytes >= delta.target_size_bytes:
        return None
    return delta


async def build_stage_urls_and_delta(
    session: AsyncSession,
    *,
    device: Device,
    project_id: uuid.UUID,
    sha256: str,
    ttl_s: int,
    settings: Settings,
    mint_url: Callable[..., str],
    supports_relay_kwarg: bool = True,
) -> tuple[str, dict[str, Any] | None]:
    """Mint `(stage_url, optional_delta_dict)` routing via parent UGV relay when present."""
    relay_url = await resolve_parent_relay_url(session, device=device, needed_sha256=sha256)
    url = (
        mint_url(sha256, settings, ttl_s=ttl_s, device=device, relay_base_url=relay_url)
        if supports_relay_kwarg
        else mint_url(sha256, settings, ttl_s=ttl_s, device=device)
    )
    delta_row = await resolve_delta_for_device(
        session,
        device=device,
        project_id=project_id,
        to_sha256=sha256,
    )
    if delta_row is None:
        return url, None

    delta_relay = await resolve_parent_relay_url(
        session, device=device, needed_sha256=delta_row.patch_sha256
    )
    delta_url = (
        mint_url(
            delta_row.patch_sha256,
            settings,
            ttl_s=ttl_s,
            device=device,
            relay_base_url=delta_relay,
        )
        if supports_relay_kwarg
        else mint_url(delta_row.patch_sha256, settings, ttl_s=ttl_s, device=device)
    )
    return url, {
        "url": delta_url,
        "from_sha256": delta_row.from_sha256,
        "patch_sha256": delta_row.patch_sha256,
        "size": delta_row.patch_size_bytes,
        "format": delta_row.format,
    }

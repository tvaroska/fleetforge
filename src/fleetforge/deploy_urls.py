"""Single artifact download URL-minting policy for devices and gateways (`R1-be-3`, `A-29`).

`mint_deploy_artifact_url` is the sole `mint_artifact_url` call site across the codebase
so low-heap HTTP fallback, parent-gateway edge-relay origin routing, and missing-config
logging stay in one transport-agnostic module outside `fleetforge.api`.
"""

from __future__ import annotations

import logging

from fleetforge.artifact_urls import mint_artifact_url
from fleetforge.config import Settings
from fleetforge.db.models import Device

logger = logging.getLogger(__name__)

LOW_HEAP_HTTP_FALLBACK_BYTES = 65536

URL_NOT_CONFIGURED = (
    "this server cannot hand out firmware download links, so no board could fetch the "
    "image; the server log names what is missing. Nothing was deployed."
)


class ArtifactUrlNotConfiguredError(RuntimeError):
    """Raised when `ARTIFACT_URL_SECRET` or `PUBLIC_BASE_URL` is absent."""


def _require_url_secret(settings: Settings) -> str:
    if not settings.artifact_url_secret:
        logger.error(
            "ARTIFACT_URL_SECRET is not set: a deploy cannot mint a download URL, so "
            "POST /v1/devices/{id}/deploy answers 503. Mint one with `just artifact-secret`."
        )
        raise ArtifactUrlNotConfiguredError(URL_NOT_CONFIGURED)
    return settings.artifact_url_secret


def _require_public_base_url(settings: Settings) -> str:
    if not settings.public_base_url:
        logger.error(
            "PUBLIC_BASE_URL is not set: a deploy cannot build an absolute download URL, "
            "so POST /v1/devices/{id}/deploy answers 503. Set the origin a DEVICE reaches."
        )
        raise ArtifactUrlNotConfiguredError(URL_NOT_CONFIGURED)
    return settings.public_base_url


def mint_deploy_artifact_url(
    sha256: str,
    settings: Settings,
    *,
    ttl_s: int | None = None,
    device: Device | None = None,
    relay_base_url: str | None = None,
) -> str:
    """Mint the public (or parent-gateway edge-relay) download URL for `sha256`."""
    base_url = relay_base_url.rstrip("/") if relay_base_url else _require_public_base_url(settings)
    use_http = (
        device is not None
        and device.free_heap is not None
        and 0 < device.free_heap < LOW_HEAP_HTTP_FALLBACK_BYTES
        and base_url.startswith("https://")
    )
    if use_http:
        base_url = "http://" + base_url.removeprefix("https://")
    url = mint_artifact_url(
        base_url,
        sha256,
        secret=_require_url_secret(settings),
        ttl_s=ttl_s if ttl_s is not None else settings.signed_url_ttl_s,
    )
    return f"{url}&proto=http" if use_http else url

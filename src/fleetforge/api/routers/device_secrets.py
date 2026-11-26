"""Device-authenticated secrets sync — `GET /v1/device-secrets` (`SEC-be-1`).

When a project's secrets revision (`dn/cfg` -> `secrets_rev`) advances past a board's
locally stored revision (`NVS "ff_sec"`), the firmware fetches the latest key-value
dictionary over HTTPS from `GET /v1/device-secrets` authenticated with its enrolled
per-device credential (`Authorization: Basic <base64(device_id:mqtt_password)>`).

Why HTTPS pull instead of putting plaintext secret values in `dn/cfg` on MQTT:
* Retained MQTT payloads sit in the broker's persistence store in plaintext and are
  replayed on every subscription.
* `dn/cfg` stays tiny (`{"stay_awake": false, "secrets_rev": 3}`) and acts purely as a
  cache-invalidation doorbell.
* Only live (non-decommissioned) boards belonging to that project can pull the project's
  decrypted secrets over TLS.
"""

import logging

from fastapi import APIRouter, HTTPException, Request, status
from sqlalchemy import select

from fleetforge.api.deps import SessionMakerDep, SettingsDep
from fleetforge.api.router_helpers import (
    INVALID_DEVICE_CREDENTIAL as INVALID_DEVICE_CREDENTIAL,
)
from fleetforge.api.router_helpers import (
    authenticate_device_basic,
)
from fleetforge.api.router_helpers import (
    parse_device_basic_auth as _parse_basic_auth,
)
from fleetforge.api.schemas import DeviceSecretsResponse
from fleetforge.db.models import Project, ProjectSecret
from fleetforge.secrets_crypto import decrypt_secret

logger = logging.getLogger(__name__)

router = APIRouter(prefix="/v1", tags=["devices"])


@router.get(
    "/device-secrets",
    response_model=DeviceSecretsResponse,
    summary="Fetch decrypted project secrets for an enrolled device",
)
async def get_device_secrets(
    request: Request,
    settings: SettingsDep,
    sessionmaker: SessionMakerDep,
) -> DeviceSecretsResponse:
    """Return the device's project `secrets_rev` and decrypted key-value secrets."""
    auth_header = request.headers.get("Authorization")
    parsed = _parse_basic_auth(auth_header)
    if parsed is None:
        raise INVALID_DEVICE_CREDENTIAL
    device_id, _ = parsed

    async with sessionmaker() as session:
        device = await authenticate_device_basic(session, auth_header)
        if device is None:
            logger.info("device-secrets rejected for device_id=%s", device_id)
            raise INVALID_DEVICE_CREDENTIAL

        project = await session.get(Project, device.project_id)
        if project is None:
            raise INVALID_DEVICE_CREDENTIAL

        rows = (
            await session.scalars(
                select(ProjectSecret)
                .where(ProjectSecret.project_id == project.id)
                .order_by(ProjectSecret.key.asc())
            )
        ).all()

    secrets_map: dict[str, str] = {}
    for row in rows:
        try:
            secrets_map[row.key] = decrypt_secret(row.ciphertext, settings)
        except ValueError:
            logger.error(
                "device-secrets: failed to decrypt secret key=%s for project=%s",
                row.key,
                project.slug,
            )
            raise HTTPException(
                status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
                detail="secret decryption failed",
            ) from None

    return DeviceSecretsResponse(
        secrets_rev=project.secrets_rev,
        secrets=secrets_map,
    )

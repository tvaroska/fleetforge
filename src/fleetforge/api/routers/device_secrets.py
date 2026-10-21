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

import base64
import binascii
import hashlib
import hmac
import logging

from fastapi import APIRouter, HTTPException, Request, status
from sqlalchemy import select

from fleetforge.api.deps import SessionMakerDep, SettingsDep
from fleetforge.api.schemas import DeviceSecretsResponse
from fleetforge.db.models import Device, Project, ProjectSecret
from fleetforge.identity import is_valid_device_id
from fleetforge.secrets_crypto import decrypt_secret

logger = logging.getLogger(__name__)

router = APIRouter(prefix="/v1", tags=["devices"])

INVALID_DEVICE_CREDENTIAL = HTTPException(
    status_code=status.HTTP_401_UNAUTHORIZED,
    detail="invalid device credentials",
    headers={"WWW-Authenticate": "Basic"},
)


def _parse_basic_auth(auth_header: str | None) -> tuple[str, str] | None:
    """Extract `(device_id, mqtt_password)` from an HTTP Basic `Authorization` header."""
    if not auth_header:
        return None
    scheme, _, param = auth_header.strip().partition(" ")
    if scheme.lower() != "basic" or not param:
        return None
    try:
        decoded = base64.b64decode(param.strip(), validate=True).decode("utf-8")
    except (binascii.Error, UnicodeDecodeError):
        return None
    if ":" not in decoded:
        return None
    device_id, password = decoded.split(":", 1)
    if not is_valid_device_id(device_id) or not password:
        return None
    return device_id, password


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
    parsed = _parse_basic_auth(request.headers.get("Authorization"))
    if parsed is None:
        raise INVALID_DEVICE_CREDENTIAL
    device_id, password = parsed
    candidate_hash = hashlib.sha256(password.encode("utf-8")).hexdigest()

    async with sessionmaker() as session:
        device = (
            await session.scalars(
                select(Device).where(
                    Device.device_id == device_id,
                    Device.decommissioned_at.is_(None),
                )
            )
        ).one_or_none()
        if (
            device is None
            or not device.mqtt_password_hash
            or not hmac.compare_digest(candidate_hash, device.mqtt_password_hash)
        ):
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
            logger.warning(
                "device-secrets: failed to decrypt secret key=%s for project=%s",
                row.key,
                project.slug,
            )

    return DeviceSecretsResponse(
        secrets_rev=project.secrets_rev,
        secrets=secrets_map,
    )

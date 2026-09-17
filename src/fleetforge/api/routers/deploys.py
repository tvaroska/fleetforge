"""`POST /v1/devices/{device_id}/deploy` — stage a version onto one board. R1-be-2.

`spec/flows.md` Flow 2 in one endpoint: resolve the label against the *device's* chip,
mint a short-lived URL for the bytes, record the intent, publish one `stage` command.
Everything after that belongs to the device.

**The server orchestrates; it never knows how or when.** There is no scheduler here, no
retry loop, no `BackgroundTasks`, no timeout on `awaiting_safe_window` — a vehicle in
motion sits there for a week and the record stays honest (`design/architecture.md`
principle 5; `deploys.py` spells out the invariant). The only server-authored terminal
state is a publish that failed, and `tests/test_api_deploy.py` has a source tripwire
that keeps it that way.

**Write order: mint → INSERT `requested` + commit → publish.** Record the intent, then
act — the same posture as enrolment's "commit, then provision". A crash between the two
leaves an honest "requested, never observed" transaction; the reverse would leave a board
downloading firmware nothing recorded.

**The URL in the command is ours now, and it is minted locally (R1-be-3).** It points at
`GET /v1/artifact/{sha256}/bin?exp=…&sig=…` on this server — the shape
`spec/device-protocol.md` already fixed — and carries an HMAC from
`fleetforge/artifact_urls.py`, no object store involved. Signing an *upstream* store URL
(an IAM `signBlob` network call on GCS) moved to the download path, where it is cached
per artifact instead of paid per deploy.

*Consequence, deliberate:* a deploy no longer fails fast when the object store is
unreachable. Nothing in the four-verb seam can check existence cheaply (`get` downloads
the whole artifact), the `artifacts` row is the evidence the bytes were stored, and the
download endpoint answers a store outage honestly and retriably. A deploy with no URL
*configuration* is still refused — see the 503 below — because answering 202 with a URL
no board can redeem is the same lie `NullCommandPublisher` refuses to tell.

**The minted URL is a bearer credential.** It exists in exactly one place — the command
payload on the wire — and is never persisted, never logged, never in the response body.
`_artifact_url()` is the single mint site.

**`detail` is lifted verbatim into the dashboard banner** (`api.ts::detailOf`), so every
rejection below names the number or the value that failed and none of them contains a
bucket, a key, a URL or a traceback.

Validation order is deliberate: cheap and caller-fixable first (bad version), then
existence (device, artifact), then compatibility (layout, size, capability). A board
that cannot take this image must be refused before anything is minted or recorded.
"""

import logging
from dataclasses import dataclass
from typing import Any

from fastapi import APIRouter, Depends, HTTPException, status
from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncSession

from fleetforge.api.deps import (
    AdminDep,
    CommandPublisherDep,
    SessionMakerDep,
    SettingsDep,
    bearer_scheme,
    cookie_scheme,
)
from fleetforge.api.routers.artifacts import VERSION_PATTERN
from fleetforge.api.schemas import DeployAccepted, DeployRequest
from fleetforge.artifact_urls import mint_artifact_url
from fleetforge.broker import CommandPublisher, CommandPublishError, new_command_id, stage_payload
from fleetforge.clock import now_utc
from fleetforge.config import Settings
from fleetforge.db.models import Device
from fleetforge.deploys import latest_open_transaction, record_publish_failure, record_requested
from fleetforge.presence import is_online

logger = logging.getLogger(__name__)

router = APIRouter(
    prefix="/v1/devices",
    tags=["deploys"],
    dependencies=[Depends(bearer_scheme), Depends(cookie_scheme)],
)

# The artifact behind a label, for the device's own chip. One statement rather than two
# reads: the join is what makes "`target` is the device's `platform_type`, never a
# request field" structural instead of a comment.
_ARTIFACT_FOR_TARGET_SQL = text(
    """
    SELECT a.sha256, a.size_bytes, a.partition_layout
      FROM artifact_versions AS v
      JOIN artifacts AS a ON a.sha256 = v.sha256
     WHERE v.target = :target AND v.version = :version
    """
)

OTA_CAPABILITY = "ota"


@dataclass(frozen=True, slots=True)
class _ResolvedArtifact:
    """The bytes a label names, for one chip. Read once, used by every check below."""

    sha256: str
    size_bytes: int
    partition_layout: str | None


URL_NOT_CONFIGURED = (
    "this server cannot hand out firmware download links, so no board could fetch the "
    "image; the server log names what is missing. Nothing was deployed."
)


def _artifact_url(sha256: str, settings: Settings) -> str:
    """Mint the public download URL for this artifact. **The only mint site.**

    Local HMAC, no I/O and nothing to fail transiently (R1-be-3): the URL points at this
    server's `GET /v1/artifact/{sha256}/bin`, and the object store is only reached when a
    device redeems it. `artifact_urls.py` owns the shape and the signing rules; nothing
    here builds a path or a query string by hand.

    The return value is a credential. It goes into the command payload and nowhere else.
    """
    return mint_artifact_url(
        _public_base_url(settings),
        sha256,
        secret=_url_secret(settings),
        ttl_s=settings.signed_url_ttl_s,
    )


def _url_secret(settings: Settings) -> str:
    """`artifact_url_secret`, or a 503 that names neither setting in the body."""
    if not settings.artifact_url_secret:
        logger.error(
            "ARTIFACT_URL_SECRET is not set: a deploy cannot mint a download URL, so "
            "POST /v1/devices/{id}/deploy answers 503. Mint one with `just artifact-secret`."
        )
        raise HTTPException(
            status_code=status.HTTP_503_SERVICE_UNAVAILABLE, detail=URL_NOT_CONFIGURED
        )
    return settings.artifact_url_secret


def _public_base_url(settings: Settings) -> str:
    """`public_base_url`, or the same 503.

    Separate from the secret because the operator's fix is different — one is minted,
    the other is the origin a board can reach — and the log line has to say which.
    """
    if not settings.public_base_url:
        logger.error(
            "PUBLIC_BASE_URL is not set: a deploy cannot build an absolute download URL, "
            "so POST /v1/devices/{id}/deploy answers 503. Set the origin a DEVICE reaches."
        )
        raise HTTPException(
            status_code=status.HTTP_503_SERVICE_UNAVAILABLE, detail=URL_NOT_CONFIGURED
        )
    return settings.public_base_url


@router.post(
    "/{device_id}/deploy",
    response_model=DeployAccepted,
    status_code=status.HTTP_202_ACCEPTED,
    summary="Stage a firmware version onto one device",
)
async def deploy_device(
    device_id: str,
    body: DeployRequest,
    admin: AdminDep,
    settings: SettingsDep,
    sessionmaker: SessionMakerDep,
    publisher: CommandPublisherDep,
) -> DeployAccepted:
    """Publish one `stage` command and return 202 with the transaction's `cmd_id`.

    A repeat of the same request while the first is still in flight **reuses** the
    `cmd_id` and republishes (`reused: true`): the device deduplicates on the id, so the
    board drops the second copy and downloads once. A different artifact is always a new
    transaction.

    202, not 201: under MQTT 3.1.1 an accepted publish is the most the server can
    honestly claim — a denied one is invisible (`broker/commands.py`). Progress arrives
    later as `up/status`.
    """
    if not VERSION_PATTERN.match(body.version):
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail=(
                "version must be 1-64 characters of letters, digits, '.', '_', '+' or "
                "'-', starting with a letter or digit"
            ),
        )

    async with sessionmaker() as session:
        device = await session.get(Device, device_id)
        if device is None or device.decommissioned_at is not None:
            # One answer for both, deliberately: a decommissioned board is gone from
            # the fleet view and from the ingestor, and a deploy to it is the same
            # mistake as a deploy to a device id that was never enrolled.
            raise HTTPException(
                status_code=status.HTTP_404_NOT_FOUND, detail=f"no such device: {device_id}"
            )

        row = (
            await session.execute(
                _ARTIFACT_FOR_TARGET_SQL,
                {"target": device.platform_type, "version": body.version},
            )
        ).first()
        if row is None:
            raise HTTPException(
                status_code=status.HTTP_404_NOT_FOUND,
                detail=(
                    f"no {device.platform_type} artifact labelled {body.version}. "
                    "The target is this device's chip, not a choice: upload the image "
                    f"built for {device.platform_type} under that version."
                ),
            )
        artifact = _ResolvedArtifact(
            sha256=row.sha256, size_bytes=row.size_bytes, partition_layout=row.partition_layout
        )

        _check_compatible(device, artifact, version=body.version)

        # The reuse window is the minted URL's lifetime: past it the first URL has
        # expired, so a board that never acted on the first command cannot act on it
        # now and a fresh intent is the honest record (`deploys.py`).
        open_transaction = await latest_open_transaction(
            session, device_id=device_id, window_s=settings.signed_url_ttl_s
        )
        reused = open_transaction is not None and open_transaction.sha256 == artifact.sha256
        cmd_id = open_transaction.cmd_id if reused and open_transaction else new_command_id()

        # Before any row exists: a deploy that could not mint a URL never happened.
        url = _artifact_url(artifact.sha256, settings)

        if not reused:
            await record_requested(
                session,
                device_id=device_id,
                cmd_id=cmd_id,
                artifact_version=body.version,
                from_version=device.fw_version,
                sha256=artifact.sha256,
                size_bytes=artifact.size_bytes,
                target=device.platform_type,
                apply=body.apply,
            )
            # Committed BEFORE the publish: record the intent, then act.
            await session.commit()

        payload = stage_payload(
            cmd_id=cmd_id,
            url=url,
            sha256=artifact.sha256,
            size=artifact.size_bytes,
            version=body.version,
            apply=body.apply,
            confirm_timeout_s=settings.confirm_timeout_s,
        )
        await _publish(
            publisher,
            session,
            payload,
            device=device,
            cmd_id=cmd_id,
            version=body.version,
        )

        online = is_online(device, now=now_utc(), tolerance=settings.presence_tolerance)

    logger.info(
        "deploy %s: %s -> %s (%s, %d bytes, apply=%s, reused=%s) requested by %s",
        cmd_id,
        device_id,
        body.version,
        artifact.sha256,
        artifact.size_bytes,
        body.apply,
        reused,
        admin.token_id,
    )
    return DeployAccepted(
        cmd_id=cmd_id,
        device_id=device_id,
        version=body.version,
        sha256=artifact.sha256,
        size_bytes=artifact.size_bytes,
        apply=body.apply,
        reused=reused,
        # Reported, never blocking: the broker queues the QoS-1 command in the device's
        # persistent session. (Which only exists once a board has connected at least
        # once with `clean_session=false` — a board that has never been seen at all
        # will not get this command on its first boot.)
        device_online=online,
    )


def _check_compatible(device: Device, artifact: _ResolvedArtifact, *, version: str) -> None:
    """Refuse an image this board cannot take, naming what did not match.

    Three independent reasons, all 409, all checked before anything is minted:
    the partition layout, the OTA slot size, and the announced `ota` capability. The
    layout and the slot size are only checked when both sides are known — an R0 board
    that announced neither is not refused for being old.
    """
    if (
        device.partition_layout is not None
        and artifact.partition_layout is not None
        and device.partition_layout != artifact.partition_layout
    ):
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT,
            detail=(
                f"this device runs partition layout {device.partition_layout} and "
                f"{version} was built for {artifact.partition_layout}. "
                "An image written into the wrong partition table does not boot."
            ),
        )

    if device.ota_slot_size is not None and artifact.size_bytes > device.ota_slot_size:
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT,
            detail=(
                f"{version} is {artifact.size_bytes} bytes and this device's OTA slot "
                f"is {device.ota_slot_size}. The image must fit the slot it is "
                "written into."
            ),
        )

    if OTA_CAPABILITY not in device.capabilities:
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT,
            detail=(
                "this device did not announce the `ota` capability, so it has no agent "
                "that can stage an update. It announced: "
                f"{', '.join(device.capabilities) or 'nothing'}."
            ),
        )


async def _publish(
    publisher: CommandPublisher,
    session: AsyncSession,
    payload: dict[str, Any],
    *,
    device: Device,
    cmd_id: str,
    version: str,
) -> None:
    """Hand the command to the broker; on failure close the transaction and 503.

    The publish-failure row is the **one** terminal state the server may author
    (`deploys.py::record_publish_failure`): the device provably never saw this command,
    so leaving the `requested` row open forever would misreport a fleet-safety loss as a
    deploy still in flight.
    """
    try:
        await publisher.publish(device.device_id, payload)
    except CommandPublishError:
        logger.exception("deploy %s for %s could not be published", cmd_id, device.device_id)
        await record_publish_failure(
            session,
            device_id=device.device_id,
            cmd_id=cmd_id,
            artifact_version=version,
            from_version=device.fw_version,
        )
        await session.commit()
        raise HTTPException(
            status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
            detail="the broker could not accept the deploy command; try again shortly",
        ) from None

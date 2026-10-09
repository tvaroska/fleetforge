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

**The pre-check (R2b-be-2)** is `POST /{device_id}/deploy/precheck`: the same body, the
same reasons, answered 200 with all of them at once and nothing sent. Every sentence lives
in `fleetforge/deploy_precheck.py`, which this endpoint turns into its first HTTP error.
The pre-check has no side effects, and its plain warnings (offline, sleepy) are never
enforced.

**The gate (R2b-be-7).** A gating warning (`needs_override`, today only
`rollback_incapable`) is the one warning `/deploy` enforces: after the refusals (a refusal
always wins, and `override` never clears one), `_accept` answers 409 with the warning's
own sentence unless the body names its code in `override`. It runs before the reuse
lookup, the mint and the INSERT, so a gated deploy writes no row and publishes nothing.
The pre-check ignores `override` and always reports the warning, so the card can render
the tick.

**The supported layouts are the `partition_profiles` table (R3-be-2).** Both routes load a
`LayoutCatalog` in their own session, right after resolving the device and the artifact,
and hand it to `deploy_precheck.refusals`, which stays pure. The pre-check also reports
`device_partition_profile`, the board's effective layout (`catalog.resolve`), beside the
layout it announces: a board on an adopted map still announces `unknown`.

**Who sent it (R2b-be-4)** is recorded on the `requested` row (`detail.sent_by`): the
subject, the token id and a snapshot of the token's label. The sender is whoever opened
the transaction; a reuse writes no row, so the original sender stands.
"""

import logging
from collections.abc import Collection
from typing import Any

from fastapi import APIRouter, Depends, HTTPException, status
from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncSession

from fleetforge.api.deps import (
    AdminDep,
    AuthContext,
    CommandPublisherDep,
    SessionMakerDep,
    SettingsDep,
    bearer_scheme,
    cookie_scheme,
)
from fleetforge.api.routers.artifacts import VERSION_PATTERN
from fleetforge.api.schemas import (
    DeployAccepted,
    DeployPrecheck,
    DeployRequest,
    PrecheckFinding,
)
from fleetforge.artifact_urls import mint_artifact_url
from fleetforge.broker import CommandPublisher, CommandPublishError, new_command_id, stage_payload
from fleetforge.clock import now_utc
from fleetforge.config import Settings
from fleetforge.db.models import AdminToken, Device
from fleetforge.deploy_precheck import (
    NO_ARTIFACT_FOR_TARGET,
    Finding,
    ResolvedArtifact,
    refusals,
    unmet_gates,
    warnings,
)
from fleetforge.deploys import (
    DeploySender,
    latest_open_transaction,
    record_publish_failure,
    record_requested,
)
from fleetforge.partition_profiles import LayoutCatalog, load_layout_catalog
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

URL_NOT_CONFIGURED = (
    "this server cannot hand out firmware download links, so no board could fetch the "
    "image; the server log names what is missing. Nothing was deployed."
)


async def _sender(session: AsyncSession, admin: AuthContext) -> DeploySender:
    """The sender record: one PK read for the token's label (never fail a deploy over it)."""
    token = await session.get(AdminToken, admin.token_id)
    return DeploySender(
        subject=admin.subject,
        token_id=str(admin.token_id),
        credential=token.name if token is not None else None,
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
    _require_valid_version(body.version)

    async with sessionmaker() as session:
        device, found_artifact = await _resolve(session, device_id, body.version)
        catalog = await load_layout_catalog(session)
        artifact = _accept(
            device, found_artifact, version=body.version, override=body.override, catalog=catalog
        )

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
            sent_by = await _sender(session, admin)
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
                sent_by=sent_by,
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
        "deploy %s: %s -> %s (%s, %d bytes, apply=%s, reused=%s, override=%s) requested by %s",
        cmd_id,
        device_id,
        body.version,
        artifact.sha256,
        artifact.size_bytes,
        body.apply,
        reused,
        body.override,
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


def _require_valid_version(version: str) -> None:
    if not VERSION_PATTERN.match(version):
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail=(
                "version must be 1-64 characters of letters, digits, '.', '_', '+' or "
                "'-', starting with a letter or digit"
            ),
        )


async def _resolve(
    session: AsyncSession, device_id: str, version: str
) -> tuple[Device, ResolvedArtifact | None]:
    """The device (404 if gone) and the artifact its chip has under `version`, if any."""
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
            {"target": device.platform_type, "version": version},
        )
    ).first()
    if row is None:
        return device, None
    return device, ResolvedArtifact(
        sha256=row.sha256, size_bytes=row.size_bytes, partition_layout=row.partition_layout
    )


def _accept(
    device: Device,
    artifact: ResolvedArtifact | None,
    *,
    version: str,
    override: Collection[str],
    catalog: LayoutCatalog,
) -> ResolvedArtifact:
    """Return the artifact, or raise the first refusal: 404 for no artifact, else 409.

    Then the gate: the first gating warning `override` does not name is a 409 with its
    own sentence. Refusals come first, so `override` can never clear one.
    """
    found: list[Finding] = refusals(device, artifact, version=version, catalog=catalog)
    if found:
        raise HTTPException(
            status_code=(
                status.HTTP_404_NOT_FOUND
                if found[0].code == NO_ARTIFACT_FOR_TARGET
                else status.HTTP_409_CONFLICT
            ),
            detail=found[0].message,
        )
    gated = unmet_gates(device, override)
    if gated:
        raise HTTPException(status_code=status.HTTP_409_CONFLICT, detail=gated[0].message)
    if artifact is None:  # unreachable: a missing artifact is always a refusal
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="no such artifact")
    return artifact


@router.post(
    "/{device_id}/deploy/precheck",
    response_model=DeployPrecheck,
    summary="Dry-run a deploy: what would be refused or warned, without sending",
)
async def precheck_deploy(
    device_id: str,
    body: DeployRequest,
    admin: AdminDep,
    settings: SettingsDep,
    sessionmaker: SessionMakerDep,
) -> DeployPrecheck:
    """Answer what `POST /deploy` would refuse and warn about, and send nothing.

    Read-only: no URL is minted, no row written, nothing sent. Request-level errors
    keep their status (400 bad version, 404 unknown device); every other reason is in the
    200 body.
    """
    _require_valid_version(body.version)

    async with sessionmaker() as session:
        device, artifact = await _resolve(session, device_id, body.version)
        catalog = await load_layout_catalog(session)
        online = is_online(device, now=now_utc(), tolerance=settings.presence_tolerance)
        found = refusals(device, artifact, version=body.version, catalog=catalog)
        warned = warnings(device, online=online)
        response = DeployPrecheck(
            device_id=device_id,
            target=device.platform_type,
            version=body.version,
            from_version=device.fw_version,
            sha256=artifact.sha256 if artifact else None,
            size_bytes=artifact.size_bytes if artifact else None,
            artifact_partition_layout=artifact.partition_layout if artifact else None,
            device_partition_layout=device.partition_layout,
            device_partition_profile=catalog.resolve(
                device.partition_layout, device.partition_table_sha256
            ),
            ota_slot_size=device.ota_slot_size,
            power_class=str(device.power_class),
            expected_wake_interval_s=device.expected_wake_interval_s,
            device_online=online,
            confirm_timeout_s=settings.confirm_timeout_s,
            deployable=not found,
            refusals=[
                PrecheckFinding(code=f.code, message=f.message, needs_override=f.needs_override)
                for f in found
            ],
            warnings=[
                PrecheckFinding(code=f.code, message=f.message, needs_override=f.needs_override)
                for f in warned
            ],
        )

    logger.info(
        "deploy precheck %s -> %s: refused=%s warned=%s by %s",
        device_id,
        body.version,
        [f.code for f in found],
        [f.code for f in warned],
        admin.token_id,
    )
    return response


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

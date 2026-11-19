"""`POST /v1/devices/{device_id}/deploy` — stage a version onto one board. R1-be-2.

`spec/flows.md` Flow 2: resolve the label against the device's chip, run pure pre-checks
(`deploy_precheck.py`), mint a short-lived local HMAC URL (`_artifact_url` is the sole
`mint_artifact_url` call site), record `requested`, commit, and publish `stage`.
"""

import logging
import uuid
from collections.abc import Collection
from typing import Annotated, Any

from fastapi import APIRouter, Depends, HTTPException, Query, status
from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncSession

from fleetforge.api.deps import (
    AdminDep,
    AuthContext,
    CommandPublisherDep,
    SessionMakerDep,
    SettingsDep,
    bearer_scheme,
    check_project_access,
    cookie_scheme,
    resolve_project_for_request,
)
from fleetforge.api.routers.artifacts import VERSION_PATTERN
from fleetforge.api.schemas import (
    PROJECT_SLUG_PATTERN,
    DeployAccepted,
    DeployKpisSummary,
    DeployPrecheck,
    DeployRequest,
    PrecheckFinding,
)
from fleetforge.artifact_urls import mint_artifact_url
from fleetforge.broker import (
    CommandPublisher,
    CommandPublishError,
    new_command_id,
    stage_payload,
)
from fleetforge.clock import now_utc
from fleetforge.config import Settings
from fleetforge.db.models import AdminToken, Device, ProjectRole
from fleetforge.deploy_precheck import (
    NO_ARTIFACT_FOR_TARGET,
    Finding,
    ResolvedArtifact,
    apply_refusals,
    refusals,
    unmet_gates,
    warnings,
)
from fleetforge.deploys import (
    DeploySender,
    deploy_kpis,
    latest_open_transaction,
    record_publish_failure,
    record_requested,
)
from fleetforge.partition_profiles import LayoutCatalog, load_layout_catalog
from fleetforge.presence import is_online
from fleetforge.rollout import (
    DEFAULT_ROLLOUT_MAX_PARALLEL as DEFAULT_ROLLOUT_MAX_PARALLEL,
)
from fleetforge.rollout import (
    advance_project_rollout_on_confirm as advance_project_rollout_on_confirm,
)
from fleetforge.rollout import (
    deploy_version_to_project_devices as deploy_version_to_project_devices,
)
from fleetforge.rollout import (
    effective_url_ttl_s as effective_url_ttl_s,
)
from fleetforge.rollout import (
    matches_project_tracking as matches_project_tracking,
)
from fleetforge.rollout import (
    republish_open_requested_on_wake as republish_open_requested_on_wake,
)
from fleetforge.signing import SigningKeyRing
from fleetforge.swarm_gateway import build_stage_urls_and_delta

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
    SELECT a.sha256, a.size_bytes, a.partition_layout, a.has_lib_marker, a.sig, a.artifact_type,
           a.sim_status, a.sim_detail
      FROM artifact_versions AS v
      JOIN artifacts AS a ON a.sha256 = v.sha256
     WHERE v.project_id = :project_id AND v.target = :target AND v.version = :version
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


LOW_HEAP_HTTP_FALLBACK_BYTES = 65536


def _artifact_url(
    sha256: str,
    settings: Settings,
    *,
    ttl_s: int | None = None,
    device: Device | None = None,
    relay_base_url: str | None = None,
) -> str:
    """Mint the public (or parent-gateway edge-relay) download URL for `sha256`. **The only mint site.**

    Local HMAC, no I/O and nothing to fail transiently (R1-be-3): the URL points at this
    server's `GET /v1/artifact/{sha256}/bin` (or the parent UGV gateway's edge cache in `V3-1`),
    and the object store is only reached when redeemed. `artifact_urls.py` owns the shape and
    the signing rules; nothing here builds a path or a query string by hand.
    """
    base_url = relay_base_url.rstrip("/") if relay_base_url else _public_base_url(settings)
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
        secret=_url_secret(settings),
        ttl_s=ttl_s if ttl_s is not None else settings.signed_url_ttl_s,
    )
    return f"{url}&proto=http" if use_http else url


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


@router.get(
    "/kpis",
    response_model=DeployKpisSummary,
    summary="Delivery success (>= 90%) and fleet safety (= 100%) KPIs over deploy history",
)
async def get_deploy_kpis(
    admin: AdminDep,
    sessionmaker: SessionMakerDep,
    project: Annotated[
        str | None,
        Query(pattern=PROJECT_SLUG_PATTERN, description="Project slug (defaults to token/default)"),
    ] = None,
) -> DeployKpisSummary:
    """Compute both v1 KPIs (`Delivery success >= 90%`, `Fleet safety = 100%`) over the
    project's `deploy_events` history (`R6-BE-1`)."""
    async with sessionmaker() as session:
        target_project = await resolve_project_for_request(
            session,
            admin,
            project_slug=project,
            min_role=ProjectRole.VIEWER,
            required_scope="read",
        )
        kpis = await deploy_kpis(session, project_id=target_project.id)
    return DeployKpisSummary(
        total_completed=kpis.total_completed,
        confirmed=kpis.confirmed,
        rolled_back=kpis.rolled_back,
        failed=kpis.failed,
        cancelled=kpis.cancelled,
        in_flight=kpis.in_flight,
        delivery_success_pct=kpis.delivery_success_pct,
        fleet_safety_pct=kpis.fleet_safety_pct,
        safety_gap_pct=kpis.safety_gap_pct,
        delivery_target_pct=kpis.delivery_target_pct,
        safety_target_pct=kpis.safety_target_pct,
        delivery_target_met=kpis.delivery_target_met,
        safety_target_met=kpis.safety_target_met,
    )


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
        device, found_artifact = await _resolve(session, device_id, body.version, settings=settings)
        await check_project_access(
            session,
            admin,
            device.project_id,
            min_role=ProjectRole.OPERATOR,
            required_scope="deploy",
            not_found=_no_such_device(device_id),
        )
        catalog = await load_layout_catalog(session)
        artifact = _accept(
            device,
            found_artifact,
            version=body.version,
            apply=body.apply,
            override=body.override,
            catalog=catalog,
        )

        # The reuse window is the minted URL's lifetime: past it the first URL has
        # expired, so a board that never acted on the first command cannot act on it
        # now and a fresh intent is the honest record (`deploys.py`).
        ttl_s = effective_url_ttl_s(device, settings)
        open_transaction = await latest_open_transaction(
            session, device_id=device_id, window_s=ttl_s
        )
        reused = open_transaction is not None and open_transaction.sha256 == artifact.sha256
        cmd_id = open_transaction.cmd_id if reused and open_transaction else new_command_id()

        # Before any row exists: a deploy that could not mint a URL never happened.
        url, delta_payload = await build_stage_urls_and_delta(
            session,
            device=device,
            project_id=device.project_id,
            sha256=artifact.sha256,
            ttl_s=ttl_s,
            settings=settings,
            mint_url=_artifact_url,
        )

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
                override=body.override,
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
            sig=artifact.sig,
            artifact_type=artifact.artifact_type,
            delta=delta_payload,
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


def _no_such_device(device_id: str) -> HTTPException:
    """`_resolve`'s 404, also for a device in a project the caller cannot see (S0-sec-2)."""
    return HTTPException(
        status_code=status.HTTP_404_NOT_FOUND, detail=f"no such device: {device_id}"
    )


async def _resolve_artifact_for_target(
    session: AsyncSession,
    *,
    project_id: uuid.UUID,
    target: str,
    version: str,
    settings: Settings | None = None,
) -> ResolvedArtifact | None:
    """Resolve the artifact behind `(project_id, target, version)`, if any."""
    row = (
        await session.execute(
            _ARTIFACT_FOR_TARGET_SQL,
            {
                "project_id": project_id,
                "target": target,
                "version": version,
            },
        )
    ).first()
    if row is None:
        return None
    uses_dev = False
    if row.sig is not None:
        try:
            ring = SigningKeyRing.from_config(
                private_key=settings.artifact_signing_key if settings else None,
                public_keys=settings.artifact_signing_public_keys if settings else None,
            )
            uses_dev = ring.uses_dev_keys and ring.signed_by_dev_key(row.sha256, row.sig)
        except ValueError:
            uses_dev = False
    return ResolvedArtifact(
        sha256=row.sha256,
        size_bytes=row.size_bytes,
        partition_layout=row.partition_layout,
        has_lib_marker=row.has_lib_marker,
        sig=row.sig,
        artifact_type=getattr(row, "artifact_type", "app"),
        is_signed=(row.sig is not None),
        uses_dev_key_ring=uses_dev,
        sim_status=getattr(row, "sim_status", None),
        sim_detail=getattr(row, "sim_detail", None),
    )


async def _resolve(
    session: AsyncSession,
    device_id: str,
    version: str,
    *,
    settings: Settings | None = None,
) -> tuple[Device, ResolvedArtifact | None]:
    """The device (404 if gone) and the artifact its chip has under `version`, if any."""
    device = await session.get(Device, device_id)
    if device is None or device.decommissioned_at is not None:
        # One answer for both, deliberately: a decommissioned board is gone from
        # the fleet view and from the ingestor, and a deploy to it is the same
        # mistake as a deploy to a device id that was never enrolled.
        raise _no_such_device(device_id)
    return device, await _resolve_artifact_for_target(
        session,
        project_id=device.project_id,
        target=device.platform_type,
        version=version,
        settings=settings,
    )


def _accept(
    device: Device,
    artifact: ResolvedArtifact | None,
    *,
    version: str,
    apply: str,
    override: Collection[str],
    catalog: LayoutCatalog,
) -> ResolvedArtifact:
    """Return the artifact, or raise the first refusal: 404 for no artifact, else 409.

    The refusals include `apply_refusals` (R5-be-2): an `on_command` deploy to a board
    that does not announce the `apply` verb is a 409, before anything is minted or written.
    Then the gate: the first gating warning `override` does not name is a 409 with its
    own sentence. Refusals come first, so `override` can never clear one.
    """
    found: list[Finding] = [
        *refusals(device, artifact, version=version, catalog=catalog),
        *apply_refusals(device, apply),
    ]
    if found:
        raise HTTPException(
            status_code=(
                status.HTTP_404_NOT_FOUND
                if found[0].code == NO_ARTIFACT_FOR_TARGET
                else status.HTTP_409_CONFLICT
            ),
            detail=found[0].message,
        )
    gated = unmet_gates(device, override, artifact=artifact)
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
        device, artifact = await _resolve(session, device_id, body.version, settings=settings)
        await check_project_access(
            session,
            admin,
            device.project_id,
            min_role=ProjectRole.VIEWER,
            required_scope="read",
            not_found=_no_such_device(device_id),
        )
        catalog = await load_layout_catalog(session)
        online = is_online(device, now=now_utc(), tolerance=settings.presence_tolerance)
        found = [
            *refusals(device, artifact, version=body.version, catalog=catalog),
            *apply_refusals(device, body.apply),
        ]
        warned = warnings(device, online=online, artifact=artifact)
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

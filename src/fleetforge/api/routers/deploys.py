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

**The gate (R2b-be-7).** A gating warning (`needs_override`: `rollback_incapable`, and from
R3-be-1 `no_library_marker`) is the one warning `/deploy` enforces: after the refusals (a refusal
always wins, and `override` never clears one), `_accept` answers 409 with the warning's
own sentence unless the body names its code in `override`. It runs before the reuse
lookup, the mint and the INSERT, so a gated deploy writes no row and publishes nothing.
The pre-check ignores `override` and always reports the warning, so the card can render
the tick. `no_library_marker` reads the verdict `POST /v1/artifact` stored at upload
(`artifacts.has_lib_marker`), never the bytes; a NULL verdict (uploaded before R3-be-1)
never warns.

**The supported layouts are the `partition_profiles` table (R3-be-2).** Both routes load a
`LayoutCatalog` in their own session, right after resolving the device and the artifact,
and hand it to `deploy_precheck.refusals`, which stays pure. The pre-check also reports
`device_partition_profile`, the board's effective layout (`catalog.resolve`), beside the
layout it announces: a board on an adopted map still announces `unknown`.

**Who sent it (R2b-be-4)** is recorded on the `requested` row (`detail.sent_by`): the
subject, the token id and a snapshot of the token's label. The sender is whoever opened
the transaction; a reuse writes no row, so the original sender stands.
"""

import fnmatch
import logging
import uuid
from collections.abc import Collection
from typing import Annotated, Any

from fastapi import APIRouter, Depends, HTTPException, Query, status
from sqlalchemy import select, text
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
    NullCommandPublisher,
    new_command_id,
    stage_payload,
)
from fleetforge.clock import now_utc
from fleetforge.config import Settings
from fleetforge.db.models import AdminToken, Device, Project, ProjectRole
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
    latest_deploys,
    latest_open_transaction,
    record_publish_failure,
    record_requested,
    requested_rollout,
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
    SELECT a.sha256, a.size_bytes, a.partition_layout, a.has_lib_marker, a.sig
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
        device, found_artifact = await _resolve(session, device_id, body.version)
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
            sig=artifact.sig,
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


async def _resolve(
    session: AsyncSession, device_id: str, version: str
) -> tuple[Device, ResolvedArtifact | None]:
    """The device (404 if gone) and the artifact its chip has under `version`, if any."""
    device = await session.get(Device, device_id)
    if device is None or device.decommissioned_at is not None:
        # One answer for both, deliberately: a decommissioned board is gone from
        # the fleet view and from the ingestor, and a deploy to it is the same
        # mistake as a deploy to a device id that was never enrolled.
        raise _no_such_device(device_id)

    row = (
        await session.execute(
            _ARTIFACT_FOR_TARGET_SQL,
            {
                "project_id": device.project_id,
                "target": device.platform_type,
                "version": version,
            },
        )
    ).first()
    if row is None:
        return device, None
    return device, ResolvedArtifact(
        sha256=row.sha256,
        size_bytes=row.size_bytes,
        partition_layout=row.partition_layout,
        has_lib_marker=row.has_lib_marker,
        sig=row.sig,
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
        device, artifact = await _resolve(session, device_id, body.version)
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


def _normalize_repo_url(url: str | None) -> str:
    if not url:
        return ""
    stripped = url.strip().rstrip("/")
    if stripped.endswith(".git"):
        stripped = stripped[:-4]
    return stripped.lower()


def matches_project_tracking(
    project: Project,
    *,
    repo_url: str | None,
    branch: str | None,
    commit_sha: str | None,
    tag: str | None,
    test_passed: bool | None,
) -> bool:
    """Return True if an artifact upload's Git provenance matches `project`'s active CD rule."""
    if project.track_mode == "off" or project.auto_deploy_target not in ("dev", "all"):
        return False
    ref = (project.track_ref or "").strip()
    if not ref:
        return False
    if test_passed is False:
        return False
    if (
        project.repo_url
        and repo_url
        and _normalize_repo_url(project.repo_url) != _normalize_repo_url(repo_url)
    ):
        return False
    if project.track_mode == "branch":
        return branch is not None and branch == ref
    if project.track_mode == "tag":
        return tag is not None and fnmatch.fnmatchcase(tag, ref)
    if project.track_mode == "commit":
        return commit_sha is not None and (commit_sha == ref or commit_sha.startswith(ref))
    return False


DEFAULT_ROLLOUT_MAX_PARALLEL = 5


async def deploy_version_to_project_devices(
    session: AsyncSession,
    *,
    project_id: uuid.UUID,
    version: str,
    target_filter: str,
    apply: str,
    override: Collection[str],
    admin: AuthContext | None = None,
    settings: Settings,
    publisher: CommandPublisher,
    target_chip: str | None = None,
    group_id: uuid.UUID | None = None,
    best_effort_broker: bool = False,
    canary: bool = True,
    max_parallel: int = DEFAULT_ROLLOUT_MAX_PARALLEL,
    sent_by_override: DeploySender | None = None,
    republish_in_flight: bool = True,
) -> tuple[list[DeployAccepted], dict[str, str]]:
    """Stage `version` onto compatible live boards in `project_id` with staged rollout safety.

    Enforces the three cohort rollout gates (`docs/features/groups-deploy.md`):
    1. **Auto-Halt on Rollback / Failure:** If any board in the project cohort has already
       entered `rolled_back` or `failed` on `version`, halt immediately and skip remaining boards.
    2. **Wave 1 (Canary Gate):** When `canary=True` and multiple boards are eligible, if no
       board in the project has confirmed `version` yet, stage onto 1 canary board (preferring
       online `is_dev` / `always_on` boards) and hold the rest until confirmation.
    3. **Wave 2 (Bounded Concurrency):** Cap concurrent in-flight downloads for `version` at
       `max_parallel` (default 5) so a multi-board rollout does not saturate a 2.4 GHz AP.

    Used by both `POST /v1/projects/{slug}/deploy` (`best_effort_broker=False`) and automatic
    CD triggers on `POST /v1/artifact` / `PATCH /v1/projects/{slug}` (`best_effort_broker=True`).
    """
    _require_valid_version(version)
    if best_effort_broker and (
        isinstance(publisher, NullCommandPublisher)
        or not settings.artifact_url_secret
        or not settings.public_base_url
    ):
        return [], {}

    # Serialize concurrent rollout evaluations for the same project across API workers
    # so simultaneous 'confirmed' notifications cannot stage duplicate waves or exceed max_parallel.
    await session.execute(select(Project.id).where(Project.id == project_id).with_for_update())

    all_project_stmt = (
        select(Device)
        .where(Device.project_id == project_id, Device.decommissioned_at.is_(None))
        .order_by(Device.device_id)
    )
    if target_chip is not None:
        all_project_stmt = all_project_stmt.where(Device.platform_type == target_chip)
    if group_id is not None:
        all_project_stmt = all_project_stmt.where(Device.group_id == group_id)
    all_project_devices = list((await session.scalars(all_project_stmt)).all())
    if not all_project_devices:
        return [], {}

    devices = (
        [d for d in all_project_devices if d.is_dev]
        if target_filter == "dev"
        else list(all_project_devices)
    )
    if not devices:
        return [], {}

    snapshots = await latest_deploys(session, device_ids=[d.device_id for d in all_project_devices])

    # Rule 1: Auto-halt if any board in the project cohort rolled back or failed on `version`.
    for d in all_project_devices:
        snap = snapshots.get(d.device_id)
        if (
            snap is not None
            and snap.artifact_version == version
            and snap.state
            in (
                "rolled_back",
                "failed",
            )
        ):
            halt_reason = f"rollout halted: device {d.device_id} {snap.state} on version {version}"
            return [], {cand.device_id: halt_reason for cand in devices}

    has_confirmed_canary = any(
        d.fw_version == version
        or (
            (snap := snapshots.get(d.device_id)) is not None
            and snap.artifact_version == version
            and snap.state == "confirmed"
        )
        for d in all_project_devices
    )

    catalog = await load_layout_catalog(session)
    now = now_utc()
    eligible: list[tuple[Device, ResolvedArtifact, bool]] = []
    skipped: dict[str, str] = {}

    for device in devices:
        dev_snap = snapshots.get(device.device_id)
        if device.fw_version == version or (
            dev_snap is not None
            and dev_snap.artifact_version == version
            and dev_snap.state == "confirmed"
        ):
            skipped[device.device_id] = f"already running version {version}"
            continue
        if (
            not republish_in_flight
            and dev_snap is not None
            and dev_snap.artifact_version == version
            and not dev_snap.is_terminal
        ):
            skipped[device.device_id] = f"already in flight on version {version}"
            continue
        _, found_artifact = await _resolve(session, device.device_id, version)
        found_refusals: list[Finding] = [
            *refusals(device, found_artifact, version=version, catalog=catalog),
            *apply_refusals(device, apply),
        ]
        if found_refusals:
            skipped[device.device_id] = found_refusals[0].message
            continue
        gated = unmet_gates(device, override, artifact=found_artifact)
        if gated:
            skipped[device.device_id] = gated[0].message
            continue
        if found_artifact is None:
            skipped[device.device_id] = "no such artifact"
            continue
        online = is_online(device, now=now, tolerance=settings.presence_tolerance)
        eligible.append((device, found_artifact, online))

    if not eligible:
        return [], skipped

    # Prioritise any board already in-flight on `version`, then online dev boards, then
    # online always_on boards, then online boards, then deterministic device_id order.
    def _canary_priority(
        item: tuple[Device, ResolvedArtifact, bool],
    ) -> tuple[int, int, int, int, str]:
        dev, _, online = item
        snap = snapshots.get(dev.device_id)
        in_flight_this_ver = (
            snap is not None and snap.artifact_version == version and not snap.is_terminal
        )
        return (
            0 if in_flight_this_ver else 1,
            0 if online else 1,
            0 if dev.is_dev else 1,
            0 if str(dev.power_class) == "always_on" else 1,
            dev.device_id,
        )

    if canary and not has_confirmed_canary and len(eligible) > 1:
        ordered = sorted(eligible, key=_canary_priority)
        canary_item = ordered[0]
        canary_id = canary_item[0].device_id
        for dev, _, _ in ordered[1:]:
            skipped[dev.device_id] = f"waiting for canary {canary_id} to confirm {version}"
        wave_items = [canary_item]
    else:
        # Wave 2: Bounded concurrency (`max_parallel`). Count boards already in-flight on `version`
        # outside the current eligible republish set whose state is actively transferring/applying
        # or online, so offline/sleepy boards parked in requested state do not starve max_parallel.
        eligible_ids = {dev.device_id for dev, _, _ in eligible}
        active_in_flight = sum(
            1
            for d in all_project_devices
            if d.device_id not in eligible_ids
            and (snap := snapshots.get(d.device_id)) is not None
            and snap.artifact_version == version
            and not snap.is_terminal
            and (
                snap.state != "requested"
                or is_online(d, now=now, tolerance=settings.presence_tolerance)
            )
        )
        effective_cap = max(1, max_parallel)
        available_slots = max(0, effective_cap - active_in_flight)
        ordered = sorted(eligible, key=_canary_priority)
        wave_items = ordered[:available_slots]
        for dev, _, _ in ordered[available_slots:]:
            skipped[dev.device_id] = f"queued for next wave (max_parallel={effective_cap})"

    if not wave_items:
        return [], skipped

    if sent_by_override is not None:
        sent_by = sent_by_override
    elif admin is not None:
        sent_by = await _sender(session, admin)
    else:
        sent_by = DeploySender("system", "rollout", None)
    rollout_meta: dict[str, Any] = {
        "target_filter": target_filter,
        "canary": canary,
        "max_parallel": max_parallel,
        "override": list(override),
        "target_chip": target_chip,
        "group_id": str(group_id) if group_id is not None else None,
    }
    deployed: list[DeployAccepted] = []

    # First pass: resolve artifacts and record all 'requested' transactions in DB while
    # holding the Project.id FOR UPDATE lock.
    stage_tasks: list[tuple[Device, ResolvedArtifact, str, bool, bool, dict[str, Any]]] = []
    for device, found_artifact, online in wave_items:
        open_transaction = await latest_open_transaction(
            session, device_id=device.device_id, window_s=settings.signed_url_ttl_s
        )
        reused = open_transaction is not None and open_transaction.sha256 == found_artifact.sha256
        cmd_id = open_transaction.cmd_id if reused and open_transaction else new_command_id()
        url = _artifact_url(found_artifact.sha256, settings)

        if not reused:
            await record_requested(
                session,
                device_id=device.device_id,
                cmd_id=cmd_id,
                artifact_version=version,
                from_version=device.fw_version,
                sha256=found_artifact.sha256,
                size_bytes=found_artifact.size_bytes,
                target=device.platform_type,
                apply=apply,
                sent_by=sent_by,
                rollout=rollout_meta,
            )

        payload = stage_payload(
            cmd_id=cmd_id,
            url=url,
            sha256=found_artifact.sha256,
            size=found_artifact.size_bytes,
            version=version,
            apply=apply,
            confirm_timeout_s=settings.confirm_timeout_s,
            sig=found_artifact.sig,
        )
        stage_tasks.append((device, found_artifact, cmd_id, reused, online, payload))

    # Commit all recorded transactions atomically once before publishing over MQTT
    await session.commit()

    for device, found_artifact, cmd_id, reused, online, payload in stage_tasks:
        if best_effort_broker:
            try:
                await publisher.publish(device.device_id, payload)
            except CommandPublishError:
                logger.exception(
                    "auto-deploy %s for %s could not be published", cmd_id, device.device_id
                )
                await record_publish_failure(
                    session,
                    device_id=device.device_id,
                    cmd_id=cmd_id,
                    artifact_version=version,
                    from_version=device.fw_version,
                )
                await session.commit()
                skipped[device.device_id] = "broker publish failed"
                continue
        else:
            await _publish(
                publisher,
                session,
                payload,
                device=device,
                cmd_id=cmd_id,
                version=version,
            )

        deployed.append(
            DeployAccepted(
                cmd_id=cmd_id,
                device_id=device.device_id,
                version=version,
                sha256=found_artifact.sha256,
                size_bytes=found_artifact.size_bytes,
                apply=apply,
                reused=reused,
                device_online=online,
            )
        )

    return deployed, skipped


async def advance_project_rollout_on_confirm(
    session: AsyncSession,
    *,
    cmd_id: str,
    settings: Settings,
    publisher: CommandPublisher,
    device: Device | None = None,
    device_id: str | None = None,
) -> list[DeployAccepted]:
    """Advance a staged project cohort rollout when a board confirms its wave transaction."""
    resolved_device = device
    if resolved_device is None and device_id is not None:
        resolved_device = await session.get(Device, device_id)
    if resolved_device is None:
        return []

    req = await requested_rollout(session, device_id=resolved_device.device_id, cmd_id=cmd_id)
    if req is None:
        return []

    rollout_cfg = req.rollout
    target_filter = str(rollout_cfg.get("target_filter") or "all")
    canary = bool(rollout_cfg.get("canary", True))
    raw_max_parallel = rollout_cfg.get("max_parallel", DEFAULT_ROLLOUT_MAX_PARALLEL)
    max_parallel = (
        int(raw_max_parallel)
        if isinstance(raw_max_parallel, int)
        else DEFAULT_ROLLOUT_MAX_PARALLEL
    )
    raw_override = rollout_cfg.get("override")
    override = (
        tuple(str(item) for item in raw_override)
        if isinstance(raw_override, list)
        else ()
    )
    raw_chip = rollout_cfg.get("target_chip")
    target_chip = str(raw_chip) if isinstance(raw_chip, str) else None
    raw_group = rollout_cfg.get("group_id")
    group_id = uuid.UUID(raw_group) if isinstance(raw_group, str) and raw_group else None

    deployed, _ = await deploy_version_to_project_devices(
        session,
        project_id=resolved_device.project_id,
        version=req.artifact_version,
        target_filter=target_filter,
        apply=req.apply,
        override=override,
        admin=None,
        settings=settings,
        publisher=publisher,
        target_chip=target_chip,
        group_id=group_id,
        best_effort_broker=True,
        canary=canary,
        max_parallel=max_parallel,
        sent_by_override=req.sent_by,
        republish_in_flight=False,
    )
    return deployed

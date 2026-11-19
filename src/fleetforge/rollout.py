"""Staged project cohort rollout orchestration (`A-3`, `A-9`).

Owns wave selection, per-target canary gating, cohort halt checks, mid-wave publish-failure
cleanup, wake republish bounds, and periodic rollout reconciliation so router modules stay
focused on HTTP request/response translation.
"""

from __future__ import annotations

import fnmatch
import logging
import re
import uuid
from collections.abc import Callable, Collection
from typing import Any

from fastapi import HTTPException, status
from sqlalchemy import select, text
from sqlalchemy.ext.asyncio import AsyncSession

from fleetforge.api.deps import AuthContext
from fleetforge.api.schemas_deploys import DeployAccepted
from fleetforge.broker import (
    CommandPublisher,
    CommandPublishError,
    NullCommandPublisher,
    new_command_id,
    stage_payload,
)
from fleetforge.clock import now_utc
from fleetforge.config import Settings
from fleetforge.db.models import AdminToken, Device, Project
from fleetforge.deploy_precheck import (
    Finding,
    ResolvedArtifact,
    apply_refusals,
    refusals,
    unmet_gates,
)
from fleetforge.deploys import (
    DeploySender,
    DeploySnapshot,
    deploy_transaction,
    latest_deploys,
    latest_open_transaction,
    record_publish_failure,
    record_requested,
    requested_rollout,
)
from fleetforge.partition_profiles import load_layout_catalog
from fleetforge.presence import is_online
from fleetforge.signing import SigningKeyRing
from fleetforge.swarm_gateway import build_stage_urls_and_delta

logger = logging.getLogger(__name__)

DEFAULT_ROLLOUT_MAX_PARALLEL = 5
LINK_AIRTIME_MAX_PARALLEL: dict[str, int] = {
    "lora": 1,
    "ble": 1,
    "zigbee": 1,
    "thread": 2,
    "cellular": 2,
}
PARENT_GATEWAY_MAX_PARALLEL = 3
_ROLLOUT_VERSION_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._+-]{0,63}\Z")

_ARTIFACT_FOR_TARGET_SQL = text(
    """
    SELECT a.sha256, a.size_bytes, a.partition_layout, a.has_lib_marker, a.sig, a.artifact_type,
           a.sim_status, a.sim_detail
      FROM artifact_versions AS v
      JOIN artifacts AS a ON a.sha256 = v.sha256
     WHERE v.project_id = :project_id AND v.target = :target AND v.version = :version
    """
)

_RECONCILE_CANDIDATES_SQL = text(
    """
    SELECT DISTINCT ON (d.project_id, req.artifact_version)
           req.device_id,
           req.cmd_id
      FROM deploy_events AS req
      JOIN devices AS d ON d.device_id = req.device_id
     WHERE req.state = 'requested'
       AND req.cmd_id IS NOT NULL
       AND req.artifact_version IS NOT NULL
       AND req.detail ? 'rollout'
       AND d.decommissioned_at IS NULL
       AND EXISTS (
           SELECT 1
             FROM deploy_events AS conf
            WHERE conf.device_id = req.device_id
              AND conf.cmd_id = req.cmd_id
              AND conf.state = 'confirmed'
       )
     ORDER BY d.project_id, req.artifact_version, req.at DESC, req.id DESC
     LIMIT 64
    """
)


def effective_url_ttl_s(device: Device, settings: Settings) -> int:
    """Signed URL TTL for `device`: extended by `2 * expected_wake_interval_s` for sleepy boards."""
    base_ttl = int(settings.signed_url_ttl_s)
    wake_s = int(device.expected_wake_interval_s or 0)
    if str(device.power_class) == "sleepy" and wake_s > 0:
        return max(base_ttl, 2 * wake_s + base_ttl)
    return base_ttl


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


async def resolve_artifact_for_target(
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


def _is_cohort_halt_snapshot(
    snap: DeploySnapshot | None,
    version: str,
    *,
    include_cancelled: bool,
) -> bool:
    """Return True if `snap` halts cohort progression on `version` (`A-3`).

    Excludes server-authored `publish_failed` (`snap.reason == "publish_failed"`): the board
    never received the command, so a transient broker blip must not wedge retries of `version`.
    """
    if snap is None or snap.artifact_version != version:
        return False
    if snap.state == "rolled_back":
        return True
    if include_cancelled and snap.state == "cancelled":
        return True
    if snap.state == "failed" and snap.reason != "publish_failed":
        return True
    return False


async def _sender_for(session: AsyncSession, admin: AuthContext) -> DeploySender:
    token = await session.get(AdminToken, admin.token_id)
    return DeploySender(
        subject=admin.subject,
        token_id=str(admin.token_id),
        credential=token.name if token is not None else None,
    )


def _default_mint_url(
    sha256: str,
    settings: Settings,
    *,
    ttl_s: int | None = None,
    device: Device | None = None,
    relay_base_url: str | None = None,
) -> str:
    from fleetforge.api.routers.deploys import _artifact_url

    return _artifact_url(
        sha256,
        settings,
        ttl_s=ttl_s,
        device=device,
        relay_base_url=relay_base_url,
    )


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
    mint_url: Callable[..., str] | None = None,
) -> tuple[list[DeployAccepted], dict[str, str]]:
    """Stage `version` onto compatible live boards in `project_id` with staged rollout safety."""
    if not _ROLLOUT_VERSION_RE.match(version):
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail=(
                "version must be 1-64 characters of letters, digits, '.', '_', '+' or "
                "'-', starting with a letter or digit"
            ),
        )
    if best_effort_broker and (
        isinstance(publisher, NullCommandPublisher)
        or not settings.artifact_url_secret
        or not settings.public_base_url
    ):
        return [], {}

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

    include_cancelled = not republish_in_flight
    for d in all_project_devices:
        snap = snapshots.get(d.device_id)
        if snap is not None and _is_cohort_halt_snapshot(
            snap, version, include_cancelled=include_cancelled
        ):
            halt_reason = f"rollout halted: device {d.device_id} {snap.state} on version {version}"
            return [], {cand.device_id: halt_reason for cand in devices}

    # Per-target canary (`A-3`): confirming `version` on `esp32` unlocks Wave 2 for `esp32`,
    # never for `esp32s3` or `esp32c6`.
    confirmed_targets = {
        d.platform_type
        for d in all_project_devices
        if d.fw_version == version
        or (
            (snap := snapshots.get(d.device_id)) is not None
            and snap.artifact_version == version
            and snap.state == "confirmed"
        )
    }

    catalog = await load_layout_catalog(session)
    now = now_utc()
    eligible: list[tuple[Device, ResolvedArtifact, bool]] = []
    skipped: dict[str, str] = {}
    artifacts_by_target: dict[str, ResolvedArtifact | None] = {}

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
        if device.platform_type not in artifacts_by_target:
            artifacts_by_target[device.platform_type] = await resolve_artifact_for_target(
                session,
                project_id=project_id,
                target=device.platform_type,
                version=version,
                settings=settings,
            )
        found_artifact = artifacts_by_target[device.platform_type]
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

    def _is_stalled_in_flight(dev: Device, snap: DeploySnapshot | None) -> bool:
        if snap is None or snap.artifact_version != version or snap.is_terminal:
            return False
        if is_online(dev, now=now, tolerance=settings.presence_tolerance):
            return False
        return (now - snap.at).total_seconds() >= settings.confirm_timeout_s

    def _canary_priority(
        item: tuple[Device, ResolvedArtifact, bool],
    ) -> tuple[int, int, int, int, str]:
        dev, _, online = item
        snap = snapshots.get(dev.device_id)
        in_flight_this_ver = (
            snap is not None
            and snap.artifact_version == version
            and not snap.is_terminal
            and not _is_stalled_in_flight(dev, snap)
        )
        return (
            0 if in_flight_this_ver else 1,
            0 if online else 1,
            0 if dev.is_dev else 1,
            0 if str(dev.power_class) == "always_on" else 1,
            dev.device_id,
        )

    by_target: dict[str, list[tuple[Device, ResolvedArtifact, bool]]] = {}
    for item in eligible:
        by_target.setdefault(item[0].platform_type, []).append(item)

    post_canary: list[tuple[Device, ResolvedArtifact, bool]] = []
    for plat in sorted(by_target):
        plat_ordered = sorted(by_target[plat], key=_canary_priority)
        # Also check whether another board of `plat` in the cohort is ALREADY in-flight as canary!
        plat_eligible_ids = {d.device_id for d, _, _ in plat_ordered}
        existing_plat_canary = next(
            (
                d.device_id
                for d in all_project_devices
                if d.platform_type == plat
                and d.device_id not in plat_eligible_ids
                and (snap := snapshots.get(d.device_id)) is not None
                and snap.artifact_version == version
                and not snap.is_terminal
                and not _is_stalled_in_flight(d, snap)
            ),
            None,
        )
        if canary and plat not in confirmed_targets:
            if existing_plat_canary is not None:
                for dev, _, _ in plat_ordered:
                    skipped[dev.device_id] = (
                        f"waiting for canary {existing_plat_canary} to confirm {version}"
                    )
            elif len(plat_ordered) > 1:
                canary_item = plat_ordered[0]
                canary_id = canary_item[0].device_id
                for dev, _, _ in plat_ordered[1:]:
                    skipped[dev.device_id] = f"waiting for canary {canary_id} to confirm {version}"
                post_canary.append(canary_item)
            else:
                post_canary.extend(plat_ordered)
        else:
            post_canary.extend(plat_ordered)

    if not post_canary:
        return [], skipped

    eligible_ids = {dev.device_id for dev, _, _ in post_canary}
    active_in_flight = 0
    in_flight_by_link: dict[str, int] = {}
    in_flight_by_parent: dict[str, int] = {}
    for d in all_project_devices:
        if d.device_id in eligible_ids:
            continue
        snap = snapshots.get(d.device_id)
        if (
            snap is not None
            and snap.artifact_version == version
            and not snap.is_terminal
            and not _is_stalled_in_flight(d, snap)
            and (
                snap.state != "requested"
                or is_online(d, now=now, tolerance=settings.presence_tolerance)
            )
        ):
            active_in_flight += 1
            in_flight_by_link[d.link_type] = in_flight_by_link.get(d.link_type, 0) + 1
            if d.parent_device_id:
                in_flight_by_parent[d.parent_device_id] = (
                    in_flight_by_parent.get(d.parent_device_id, 0) + 1
                )

    effective_cap = max(1, max_parallel)
    ordered = sorted(post_canary, key=_canary_priority)
    wave_items: list[tuple[Device, ResolvedArtifact, bool]] = []
    for dev, found_art, online in ordered:
        if active_in_flight >= effective_cap:
            skipped[dev.device_id] = f"queued for next wave (max_parallel={effective_cap})"
            continue
        link_cap = min(
            effective_cap,
            LINK_AIRTIME_MAX_PARALLEL.get(dev.link_type, effective_cap),
        )
        if in_flight_by_link.get(dev.link_type, 0) >= link_cap:
            skipped[dev.device_id] = (
                f"queued for next wave (airtime cap={link_cap} on {dev.link_type} link)"
            )
            continue
        if dev.parent_device_id:
            gw_cap = min(effective_cap, PARENT_GATEWAY_MAX_PARALLEL)
            if in_flight_by_parent.get(dev.parent_device_id, 0) >= gw_cap:
                skipped[dev.device_id] = (
                    f"queued for next wave (gateway {dev.parent_device_id} airtime cap={gw_cap})"
                )
                continue
        wave_items.append((dev, found_art, online))
        active_in_flight += 1
        in_flight_by_link[dev.link_type] = in_flight_by_link.get(dev.link_type, 0) + 1
        if dev.parent_device_id:
            in_flight_by_parent[dev.parent_device_id] = (
                in_flight_by_parent.get(dev.parent_device_id, 0) + 1
            )

    if not wave_items:
        return [], skipped

    if sent_by_override is not None:
        sent_by = sent_by_override
    elif admin is not None:
        sent_by = await _sender_for(session, admin)
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
    url_builder = mint_url or _default_mint_url
    deployed: list[DeployAccepted] = []

    stage_tasks: list[tuple[Device, ResolvedArtifact, str, bool, bool, dict[str, Any]]] = []
    for device, found_artifact, online in wave_items:
        ttl_s = effective_url_ttl_s(device, settings)
        open_transaction = await latest_open_transaction(
            session, device_id=device.device_id, window_s=ttl_s
        )
        reused = open_transaction is not None and open_transaction.sha256 == found_artifact.sha256
        cmd_id = open_transaction.cmd_id if reused and open_transaction else new_command_id()
        url, delta_payload = await build_stage_urls_and_delta(
            session,
            device=device,
            project_id=project_id,
            sha256=found_artifact.sha256,
            ttl_s=ttl_s,
            settings=settings,
            mint_url=url_builder,
            supports_relay_kwarg=(url_builder is _default_mint_url),
        )

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
                override=override,
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
            artifact_type=found_artifact.artifact_type,
            delta=delta_payload,
        )
        stage_tasks.append((device, found_artifact, cmd_id, reused, online, payload))

    await session.commit()

    batch_published = False
    if len(stage_tasks) > 1:
        try:
            await publisher.publish_many(
                [(dev.device_id, payload) for dev, _, _, _, _, payload in stage_tasks]
            )
            batch_published = True
        except CommandPublishError:
            logger.warning(
                "batch MQTT publish failed for project %s version %s; falling back per device",
                project_id,
                version,
                exc_info=True,
            )
            batch_published = False

    if batch_published:
        for device, found_artifact, cmd_id, reused, online, _ in stage_tasks:
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

    for idx, (device, found_artifact, cmd_id, reused, online, payload) in enumerate(stage_tasks):
        try:
            await publisher.publish(device.device_id, payload)
        except CommandPublishError:
            if best_effort_broker:
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

            # Close EVERY unpublished wave intent before answering 503 (`A-3`), so no
            # committed `requested` row is orphaned without a terminal `publish_failed`.
            logger.exception("deploy %s for %s could not be published", cmd_id, device.device_id)
            for rem_device, _, rem_cmd_id, _, _, _ in stage_tasks[idx:]:
                await record_publish_failure(
                    session,
                    device_id=rem_device.device_id,
                    cmd_id=rem_cmd_id,
                    artifact_version=version,
                    from_version=rem_device.fw_version,
                )
            await session.commit()
            raise HTTPException(
                status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
                detail="the broker could not accept the deploy command; try again shortly",
            ) from None

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
    if (
        isinstance(publisher, NullCommandPublisher)
        or not settings.artifact_url_secret
        or not settings.public_base_url
    ):
        logger.warning(
            "cannot advance staged rollout for cmd_id=%s: command publisher or artifact URL config missing",
            cmd_id,
        )
        return []
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
        int(raw_max_parallel) if isinstance(raw_max_parallel, int) else DEFAULT_ROLLOUT_MAX_PARALLEL
    )
    raw_override = rollout_cfg.get("override")
    override = tuple(str(item) for item in raw_override) if isinstance(raw_override, list) else ()
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
    from fleetforge.deploy_policies import advance_project_policies_for_version

    policy_promoted = await advance_project_policies_for_version(
        session,
        project_id=resolved_device.project_id,
        version=req.artifact_version,
        settings=settings,
        publisher=publisher,
    )
    seen_ids = {item.device_id for item in deployed}
    for item in policy_promoted:
        if item.device_id not in seen_ids:
            deployed.append(item)
            seen_ids.add(item.device_id)
    return deployed


async def republish_open_requested_on_wake(
    session: AsyncSession,
    *,
    device_id: str,
    settings: Settings,
    publisher: CommandPublisher,
) -> bool:
    """Re-mint a fresh signed URL and re-publish `stage` when a board wakes with an open `requested` deploy."""
    if (
        isinstance(publisher, NullCommandPublisher)
        or not settings.artifact_url_secret
        or not settings.public_base_url
    ):
        return False

    device = await session.get(Device, device_id)
    if device is None or device.decommissioned_at is not None:
        return False

    snapshots = await latest_deploys(session, device_ids=[device_id])
    snap = snapshots.get(device_id)
    if (
        snap is None
        or snap.state != "requested"
        or snap.is_terminal
        or not snap.artifact_version
        or not snap.cmd_id
    ):
        return False

    # Age bound (`A-3`): never re-stage an open intent older than `effective_url_ttl_s`.
    ttl_s = effective_url_ttl_s(device, settings)
    if (now_utc() - snap.at).total_seconds() > ttl_s:
        return False

    # Cohort halt check (`A-3`): if a peer board rolled back, failed, or was cancelled on this
    # version while this board slept, do not re-stage the halted version on wake.
    req = await requested_rollout(session, device_id=device_id, cmd_id=snap.cmd_id)
    group_filter: uuid.UUID | None = None
    if req is not None:
        raw_group = req.rollout.get("group_id")
        if isinstance(raw_group, str) and raw_group:
            group_filter = uuid.UUID(raw_group)

    peer_stmt = select(Device.device_id).where(
        Device.project_id == device.project_id,
        Device.platform_type == device.platform_type,
        Device.decommissioned_at.is_(None),
    )
    if group_filter is not None:
        peer_stmt = peer_stmt.where(Device.group_id == group_filter)
    peer_ids = list((await session.scalars(peer_stmt)).all())
    if peer_ids:
        peer_snaps = await latest_deploys(session, device_ids=peer_ids)
        if any(
            _is_cohort_halt_snapshot(
                peer_snaps.get(pid), snap.artifact_version, include_cancelled=True
            )
            for pid in peer_ids
        ):
            return False

    found_artifact = await resolve_artifact_for_target(
        session,
        project_id=device.project_id,
        target=device.platform_type,
        version=snap.artifact_version,
        settings=settings,
    )
    if found_artifact is None:
        return False

    tx = await deploy_transaction(session, device_id=device_id, cmd_id=snap.cmd_id)
    apply_mode = tx.apply if tx is not None and tx.apply else "auto"
    url, delta_payload = await build_stage_urls_and_delta(
        session,
        device=device,
        project_id=device.project_id,
        sha256=found_artifact.sha256,
        ttl_s=ttl_s,
        settings=settings,
        mint_url=_default_mint_url,
    )
    payload = stage_payload(
        cmd_id=snap.cmd_id,
        url=url,
        sha256=found_artifact.sha256,
        size=found_artifact.size_bytes,
        version=snap.artifact_version,
        apply=apply_mode,
        confirm_timeout_s=settings.confirm_timeout_s,
        sig=found_artifact.sig,
        artifact_type=found_artifact.artifact_type,
        delta=delta_payload,
    )
    await publisher.publish(device_id, payload)
    return True


async def reconcile_open_rollouts(
    session: AsyncSession,
    *,
    settings: Settings,
    publisher: CommandPublisher,
) -> list[DeployAccepted]:
    """Advance any confirmed cohort rollout whose confirm event was missed across a restart (`A-3`)."""
    if (
        isinstance(publisher, NullCommandPublisher)
        or not settings.artifact_url_secret
        or not settings.public_base_url
    ):
        return []

    rows = (await session.execute(_RECONCILE_CANDIDATES_SQL)).all()
    advanced_total: list[DeployAccepted] = []
    for row in rows:
        advanced = await advance_project_rollout_on_confirm(
            session,
            device_id=str(row.device_id),
            cmd_id=str(row.cmd_id),
            settings=settings,
            publisher=publisher,
        )
        advanced_total.extend(advanced)
    return advanced_total

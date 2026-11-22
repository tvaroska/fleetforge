"""Staged project cohort rollout orchestration (`A-3`, `A-9`).

Owns wave selection, per-target canary gating, cohort halt checks, mid-wave publish-failure
cleanup, wake republish bounds, and periodic rollout reconciliation so router modules stay
focused on HTTP request/response translation.
"""

from __future__ import annotations

import datetime as dt
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
    deploy_transaction,
    latest_deploys,
    latest_open_transaction,
    record_publish_failure,
    record_requested,
    requested_rollout,
)
from fleetforge.partition_profiles import load_layout_catalog
from fleetforge.presence import is_online
from fleetforge.rollout_state import (
    DEFAULT_ROLLOUT_MAX_PARALLEL as DEFAULT_ROLLOUT_MAX_PARALLEL,
)
from fleetforge.rollout_state import (
    LINK_AIRTIME_MAX_PARALLEL as LINK_AIRTIME_MAX_PARALLEL,
)
from fleetforge.rollout_state import (
    PARENT_GATEWAY_MAX_PARALLEL as PARENT_GATEWAY_MAX_PARALLEL,
)
from fleetforge.rollout_state import (
    close_matching_rollouts,
    find_open_rollout,
    has_newer_confirmed_or_in_flight,
    open_or_supersede_rollout,
    resolve_rollout_for_confirm,
    select_rollout_wave,
)
from fleetforge.rollout_state import (
    effective_url_ttl_s as effective_url_ttl_s,
)
from fleetforge.rollout_state import (
    is_cohort_halt_snapshot as _is_cohort_halt_snapshot,
)
from fleetforge.rollout_state import (
    matches_project_tracking as matches_project_tracking,
)
from fleetforge.rollout_state import (
    reconcile_open_rollouts as reconcile_open_rollouts,
)
from fleetforge.signing import SigningKeyRing
from fleetforge.swarm_gateway import build_stage_urls_and_delta

logger = logging.getLogger(__name__)

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
    policy_id: uuid.UUID | None = None,
    rollout_started_at: dt.datetime | None = None,
    ignore_halts_before: dt.datetime | None = None,
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

    if sent_by_override is not None:
        sent_by = sent_by_override
    elif admin is not None:
        sent_by = await _sender_for(session, admin)
    else:
        sent_by = DeploySender("system", "rollout", None)

    if republish_in_flight:
        rollout_row = await open_or_supersede_rollout(
            session,
            project_id=project_id,
            version=version,
            target_filter=target_filter,
            target_chip=target_chip,
            group_id=group_id,
            policy_id=policy_id,
            apply=apply,
            canary=canary,
            max_parallel=max_parallel,
            override=override,
            sent_by=sent_by,
        )
    else:
        rollout_row = await find_open_rollout(
            session,
            project_id=project_id,
            version=version,
            target_filter=target_filter,
            target_chip=target_chip,
            group_id=group_id,
            policy_id=policy_id,
        )
        if rollout_started_at is None and rollout_row is not None:
            rollout_started_at = rollout_row.created_at

    snapshots = await latest_deploys(session, device_ids=[d.device_id for d in all_project_devices])

    include_cancelled = not republish_in_flight
    for d in all_project_devices:
        snap = snapshots.get(d.device_id)
        if (
            snap is not None
            and (ignore_halts_before is None or snap.at > ignore_halts_before)
            and _is_cohort_halt_snapshot(snap, version, include_cancelled=include_cancelled)
        ):
            halt_reason = f"rollout halted: device {d.device_id} {snap.state} on version {version}"
            await close_matching_rollouts(
                session,
                project_id=project_id,
                version=version,
                target_filter=target_filter,
                target_chip=target_chip,
                group_id=group_id,
                policy_id=policy_id,
                status_value="halted",
                reason=halt_reason,
            )
            if isinstance(session, AsyncSession):
                await session.commit()
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
        if not republish_in_flight and has_newer_confirmed_or_in_flight(
            dev_snap, version=version, rollout_started_at=rollout_started_at
        ):
            skipped[device.device_id] = (
                f"already running newer rollout version {dev_snap.artifact_version}"
                if dev_snap is not None
                else "already running newer rollout version"
            )
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
        any_in_flight = any(
            (snap := snapshots.get(d.device_id)) is not None
            and snap.artifact_version == version
            and not snap.is_terminal
            for d in devices
        )
        if not any_in_flight:
            await close_matching_rollouts(
                session,
                project_id=project_id,
                version=version,
                target_filter=target_filter,
                target_chip=target_chip,
                group_id=group_id,
                policy_id=policy_id,
                status_value="completed",
                reason=None,
            )
        if isinstance(session, AsyncSession):
            await session.commit()
        return [], skipped

    wave_items = select_rollout_wave(
        eligible=eligible,
        all_project_devices=all_project_devices,
        snapshots=snapshots,
        confirmed_targets=confirmed_targets,
        version=version,
        canary=canary,
        max_parallel=max_parallel,
        now=now,
        settings=settings,
        skipped=skipped,
    )
    if not wave_items:
        if isinstance(session, AsyncSession):
            await session.commit()
        return [], skipped

    rollout_meta: dict[str, Any] = {
        "target_filter": target_filter,
        "canary": canary,
        "max_parallel": max_parallel,
        "override": list(override),
        "target_chip": target_chip,
        "group_id": str(group_id) if group_id is not None else None,
    }
    if policy_id is not None:
        rollout_meta["policy_id"] = str(policy_id)
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
    raw_policy = rollout_cfg.get("policy_id")
    policy_id = uuid.UUID(raw_policy) if isinstance(raw_policy, str) and raw_policy else None

    _, is_closed, started_at = await resolve_rollout_for_confirm(
        session,
        project_id=resolved_device.project_id,
        version=req.artifact_version,
        target_filter=target_filter,
        target_chip=target_chip,
        group_id=group_id,
        policy_id=policy_id,
        fallback_started_at=req.at,
    )
    if is_closed:
        return []

    from fleetforge.deploy_policies import (
        advance_project_policies_for_version,
        group_is_policy_governed,
    )

    if policy_id is not None or await group_is_policy_governed(
        session,
        project_id=resolved_device.project_id,
        version=req.artifact_version,
        group_id=group_id,
    ):
        return await advance_project_policies_for_version(
            session,
            project_id=resolved_device.project_id,
            version=req.artifact_version,
            settings=settings,
            publisher=publisher,
            target_policy_id=policy_id,
        )

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
        rollout_started_at=started_at,
    )
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

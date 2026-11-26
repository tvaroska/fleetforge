"""Persisted cohort rollout lifecycle and bounded background reconciliation (`A-21`).

Tracks open rollouts in `project_rollouts`, supersedes older overlapping rollouts when a
new cohort rollout starts, closes rollouts on completion/halt/cancellation, and bounds
background reconciliation so finished or superseded versions are never re-staged.
"""

from __future__ import annotations

import datetime as dt
import fnmatch
import uuid
from collections.abc import Collection
from typing import Any

from sqlalchemy import select, text
from sqlalchemy.ext.asyncio import AsyncSession

from fleetforge.broker import CommandPublisher, NullCommandPublisher
from fleetforge.clock import now_utc
from fleetforge.config import Settings
from fleetforge.db.models import DeployPolicy, DeployPolicyRun, Device, Project, ProjectRollout
from fleetforge.deploy_precheck import ResolvedArtifact
from fleetforge.deploys import DeploySender, DeploySnapshot
from fleetforge.presence import is_online
from fleetforge.rollout_types import DeployAccepted

DEFAULT_ROLLOUT_MAX_PARALLEL = 5
LINK_AIRTIME_MAX_PARALLEL: dict[str, int] = {
    "lora": 1,
    "ble": 1,
    "zigbee": 1,
    "thread": 2,
    "cellular": 2,
}
PARENT_GATEWAY_MAX_PARALLEL = 3

# Fallback for pre-0020 `deploy_events` rows with no `project_rollouts` entry: pick ONLY the
# single newest rollout per `(project_id, target_chip, group_id)` inside the age window.
_LEGACY_RECONCILE_CANDIDATES_SQL = text(
    """
    WITH ranked AS (
        SELECT req.device_id,
               req.cmd_id,
               req.artifact_version,
               req.at,
               req.id,
               d.project_id,
               ROW_NUMBER() OVER (
                   PARTITION BY d.project_id,
                                COALESCE(req.detail -> 'rollout' ->> 'target_chip', ''),
                                COALESCE(req.detail -> 'rollout' ->> 'group_id', '')
                   ORDER BY req.at DESC, req.id DESC
               ) AS rn
          FROM deploy_events AS req
          JOIN devices AS d ON d.device_id = req.device_id
         WHERE req.state = 'requested'
           AND req.cmd_id IS NOT NULL
           AND req.artifact_version IS NOT NULL
           AND req.detail ? 'rollout'
           AND d.decommissioned_at IS NULL
           AND req.at >= :cutoff
    )
    SELECT r.device_id, r.cmd_id
      FROM ranked AS r
     WHERE r.rn = 1
       AND NOT EXISTS (
           SELECT 1
             FROM project_rollouts AS pr
            WHERE pr.project_id = r.project_id
              AND pr.version = r.artifact_version
       )
       AND EXISTS (
           SELECT 1
             FROM deploy_events AS conf
            WHERE conf.device_id = r.device_id
              AND conf.cmd_id = r.cmd_id
              AND conf.state = 'confirmed'
       )
     ORDER BY r.at DESC, r.id DESC
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


def is_cohort_halt_snapshot(
    snap: DeploySnapshot | None,
    version: str,
    *,
    include_cancelled: bool,
) -> bool:
    """Return True if `snap` halts cohort progression on `version` (`A-3`)."""
    if snap is None or snap.artifact_version != version:
        return False
    if snap.state == "rolled_back":
        return True
    if include_cancelled and snap.state == "cancelled":
        return True
    if snap.state == "failed" and snap.reason != "publish_failed":
        return True
    return False


def _is_stalled_in_flight(
    dev: Device,
    snap: DeploySnapshot | None,
    *,
    now: dt.datetime,
    settings: Settings,
) -> bool:
    if snap is None or snap.is_terminal:
        return False
    if is_online(dev, now=now, tolerance=settings.presence_tolerance):
        return False
    return (now - snap.at).total_seconds() >= settings.confirm_timeout_s


def _canary_priority(
    item: tuple[Device, ResolvedArtifact, bool],
    *,
    snapshots: dict[str, DeploySnapshot],
    version: str,
    now: dt.datetime,
    settings: Settings,
) -> tuple[int, int, int, int, str]:
    dev, _, online = item
    snap = snapshots.get(dev.device_id)
    in_flight_this_ver = (
        snap is not None
        and snap.artifact_version == version
        and not snap.is_terminal
        and not _is_stalled_in_flight(dev, snap, now=now, settings=settings)
    )
    return (
        0 if in_flight_this_ver else 1,
        0 if online else 1,
        0 if dev.is_dev else 1,
        0 if str(dev.power_class) == "always_on" else 1,
        dev.device_id,
    )


def _select_post_canary(
    *,
    eligible: list[tuple[Device, ResolvedArtifact, bool]],
    all_project_devices: list[Device],
    snapshots: dict[str, DeploySnapshot],
    confirmed_targets: set[str],
    version: str,
    canary: bool,
    now: dt.datetime,
    settings: Settings,
    skipped: dict[str, str],
) -> list[tuple[Device, ResolvedArtifact, bool]]:
    by_target: dict[str, list[tuple[Device, ResolvedArtifact, bool]]] = {}
    for item in eligible:
        by_target.setdefault(item[0].platform_type, []).append(item)

    post_canary: list[tuple[Device, ResolvedArtifact, bool]] = []
    for plat in sorted(by_target):
        plat_ordered = sorted(
            by_target[plat],
            key=lambda it: _canary_priority(
                it, snapshots=snapshots, version=version, now=now, settings=settings
            ),
        )
        if not canary or plat in confirmed_targets:
            post_canary.extend(plat_ordered)
            continue
        plat_eligible_ids = {d.device_id for d, _, _ in plat_ordered}
        existing_canary = next(
            (
                d.device_id
                for d in all_project_devices
                if d.platform_type == plat
                and d.device_id not in plat_eligible_ids
                and (snap := snapshots.get(d.device_id)) is not None
                and snap.artifact_version == version
                and not snap.is_terminal
                and not _is_stalled_in_flight(d, snap, now=now, settings=settings)
            ),
            None,
        )
        if existing_canary is not None:
            for dev, _, _ in plat_ordered:
                skipped[dev.device_id] = (
                    f"waiting for canary {existing_canary} to confirm {version}"
                )
        elif len(plat_ordered) > 1:
            canary_item = plat_ordered[0]
            canary_id = canary_item[0].device_id
            for dev, _, _ in plat_ordered[1:]:
                skipped[dev.device_id] = f"waiting for canary {canary_id} to confirm {version}"
            post_canary.append(canary_item)
        else:
            post_canary.extend(plat_ordered)
    return post_canary


def _count_active_in_flight(
    *,
    all_project_devices: list[Device],
    eligible_ids: set[str],
    snapshots: dict[str, DeploySnapshot],
    version: str,
    now: dt.datetime,
    settings: Settings,
    airtime_devices: list[Device] | None = None,
) -> tuple[int, dict[str, int], dict[str, int]]:
    """Count in-flight boards: `max_parallel` for `version`, and radio/gateway airtime across ALL versions (`A-30`)."""
    active_in_flight = 0
    in_flight_by_link: dict[str, int] = {}
    in_flight_by_parent: dict[str, int] = {}
    cohort_ids = {d.device_id for d in all_project_devices}
    scan_devices = airtime_devices if airtime_devices is not None else all_project_devices
    for d in scan_devices:
        snap = snapshots.get(d.device_id)
        if (
            snap is None
            or snap.is_terminal
            or _is_stalled_in_flight(d, snap, now=now, settings=settings)
        ):
            continue
        if snap.state == "requested" and not is_online(
            d, now=now, tolerance=settings.presence_tolerance
        ):
            continue
        if snap.artifact_version == version:
            if d.device_id in eligible_ids:
                continue
            if d.device_id in cohort_ids:
                active_in_flight += 1
        in_flight_by_link[d.link_type] = in_flight_by_link.get(d.link_type, 0) + 1
        if d.parent_device_id:
            in_flight_by_parent[d.parent_device_id] = (
                in_flight_by_parent.get(d.parent_device_id, 0) + 1
            )
    return active_in_flight, in_flight_by_link, in_flight_by_parent


def select_rollout_wave(
    *,
    eligible: list[tuple[Device, ResolvedArtifact, bool]],
    all_project_devices: list[Device],
    snapshots: dict[str, DeploySnapshot],
    confirmed_targets: set[str],
    version: str,
    canary: bool,
    max_parallel: int,
    now: dt.datetime,
    settings: Settings,
    skipped: dict[str, str],
    airtime_devices: list[Device] | None = None,
) -> list[tuple[Device, ResolvedArtifact, bool]]:
    """Select canary / bounded wave candidates respecting link and parent-gateway airtime caps."""
    post_canary = _select_post_canary(
        eligible=eligible,
        all_project_devices=all_project_devices,
        snapshots=snapshots,
        confirmed_targets=confirmed_targets,
        version=version,
        canary=canary,
        now=now,
        settings=settings,
        skipped=skipped,
    )
    if not post_canary:
        return []

    eligible_ids = {dev.device_id for dev, _, _ in post_canary}
    active_in_flight, in_flight_by_link, in_flight_by_parent = _count_active_in_flight(
        all_project_devices=all_project_devices,
        eligible_ids=eligible_ids,
        snapshots=snapshots,
        version=version,
        now=now,
        settings=settings,
        airtime_devices=airtime_devices,
    )

    effective_cap = max(1, max_parallel)
    ordered = sorted(
        post_canary,
        key=lambda it: _canary_priority(
            it, snapshots=snapshots, version=version, now=now, settings=settings
        ),
    )
    wave_items: list[tuple[Device, ResolvedArtifact, bool]] = []
    for dev, found_art, online in ordered:
        if active_in_flight >= effective_cap:
            skipped[dev.device_id] = f"queued for next wave (max_parallel={effective_cap})"
            continue
        link_cap = min(effective_cap, LINK_AIRTIME_MAX_PARALLEL.get(dev.link_type, effective_cap))
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
    return wave_items


def scope_covers(
    outer_filter: str,
    outer_chip: str | None,
    outer_group: uuid.UUID | None,
    inner_filter: str,
    inner_chip: str | None,
    inner_group: uuid.UUID | None,
) -> bool:
    """Return True when the outer rollout scope completely covers the inner scope."""
    filter_ok = outer_filter == "all" or outer_filter == inner_filter
    chip_ok = outer_chip is None or outer_chip == inner_chip
    group_ok = outer_group is None or outer_group == inner_group
    return filter_ok and chip_ok and group_ok


def sender_to_json(sent_by: DeploySender) -> dict[str, Any]:
    """Serialize `DeploySender` for `project_rollouts.sent_by` JSONB."""
    return {
        "subject": sent_by.subject,
        "token_id": sent_by.token_id,
        "credential": sent_by.credential,
    }


def sender_from_json(raw: dict[str, Any] | None) -> DeploySender:
    """Deserialize `project_rollouts.sent_by` JSONB into a `DeploySender`."""
    if not isinstance(raw, dict):
        return DeploySender("system", "rollout", None)
    credential = raw.get("credential")
    return DeploySender(
        subject=str(raw.get("subject") or "system"),
        token_id=str(raw.get("token_id") or "rollout"),
        credential=credential if isinstance(credential, str) else None,
    )


def has_newer_confirmed_or_in_flight(
    snap: DeploySnapshot | None,
    *,
    version: str,
    rollout_started_at: dt.datetime | None,
) -> bool:
    """Return True when `snap` reflects a newer rollout of another version on this board.

    Used during automatic wave continuation (`republish_in_flight=False`) so a stale or
    replayed rollout never downgrades a board over a newer confirmed or in-flight version.
    """
    if snap is None or snap.artifact_version is None or snap.artifact_version == version:
        return False
    if rollout_started_at is not None and snap.at < rollout_started_at:
        return False
    return snap.state == "confirmed" or not snap.is_terminal


async def open_or_supersede_rollout(
    session: AsyncSession,
    *,
    project_id: uuid.UUID,
    version: str,
    target_filter: str,
    target_chip: str | None,
    group_id: uuid.UUID | None,
    policy_id: uuid.UUID | None,
    apply: str,
    canary: bool,
    max_parallel: int,
    override: Collection[str],
    sent_by: DeploySender,
) -> ProjectRollout | None:
    """Supersede older covered rollouts and open/refresh the active row for this rollout."""
    if not isinstance(session, AsyncSession):
        return None
    now = now_utc()
    open_rows = list(
        (
            await session.scalars(
                select(ProjectRollout).where(
                    ProjectRollout.project_id == project_id,
                    ProjectRollout.status == "open",
                )
            )
        ).all()
    )
    existing: ProjectRollout | None = None
    for row in open_rows:
        if row.version != version and scope_covers(
            target_filter,
            target_chip,
            group_id,
            row.target_filter,
            row.target_chip,
            row.group_id,
        ):
            row.status = "superseded"
            row.close_reason = f"superseded by rollout of {version}"
            row.closed_at = now
            row.updated_at = now
        elif (
            row.version == version
            and row.target_filter == target_filter
            and row.target_chip == target_chip
            and row.group_id == group_id
            and row.policy_id == policy_id
            and existing is None
        ):
            existing = row

    if existing is not None:
        existing.apply = apply
        existing.canary = canary
        existing.max_parallel = max_parallel
        existing.override = list(override)
        existing.sent_by = sender_to_json(sent_by)
        existing.updated_at = now
        await session.flush()
        return existing

    created = ProjectRollout(
        project_id=project_id,
        version=version,
        target_filter=target_filter,
        target_chip=target_chip,
        group_id=group_id,
        policy_id=policy_id,
        apply=apply,
        canary=canary,
        max_parallel=max_parallel,
        override=list(override),
        sent_by=sender_to_json(sent_by),
        status="open",
        created_at=now,
        updated_at=now,
    )
    session.add(created)
    await session.flush()
    return created


async def find_open_rollout(
    session: AsyncSession,
    *,
    project_id: uuid.UUID,
    version: str,
    target_filter: str,
    target_chip: str | None,
    group_id: uuid.UUID | None,
    policy_id: uuid.UUID | None = None,
) -> ProjectRollout | None:
    """Find the newest open `ProjectRollout` matching the given scope, if any."""
    if not isinstance(session, AsyncSession):
        return None
    stmt = (
        select(ProjectRollout)
        .where(
            ProjectRollout.project_id == project_id,
            ProjectRollout.version == version,
            ProjectRollout.target_filter == target_filter,
            ProjectRollout.status == "open",
        )
        .order_by(ProjectRollout.created_at.desc(), ProjectRollout.id.desc())
    )
    stmt = (
        stmt.where(ProjectRollout.target_chip.is_(None))
        if target_chip is None
        else stmt.where(ProjectRollout.target_chip == target_chip)
    )
    stmt = (
        stmt.where(ProjectRollout.group_id.is_(None))
        if group_id is None
        else stmt.where(ProjectRollout.group_id == group_id)
    )
    if policy_id is not None:
        stmt = stmt.where(ProjectRollout.policy_id == policy_id)
    found: ProjectRollout | None = await session.scalar(stmt.limit(1))
    return found


async def close_matching_rollouts(
    session: AsyncSession,
    *,
    project_id: uuid.UUID,
    version: str | None,
    target_filter: str,
    target_chip: str | None,
    group_id: uuid.UUID | None,
    status_value: str,
    reason: str | None,
    policy_id: uuid.UUID | None = None,
) -> int:
    """Close open `ProjectRollout` rows covered by the given scope and optional version."""
    if not isinstance(session, AsyncSession):
        return 0
    stmt = select(ProjectRollout).where(
        ProjectRollout.project_id == project_id,
        ProjectRollout.status == "open",
    )
    if version is not None:
        stmt = stmt.where(ProjectRollout.version == version)
    if policy_id is not None:
        stmt = stmt.where(ProjectRollout.policy_id == policy_id)
    rows = list((await session.scalars(stmt)).all())
    now = now_utc()
    closed = 0
    for row in rows:
        if scope_covers(
            target_filter,
            target_chip,
            group_id,
            row.target_filter,
            row.target_chip,
            row.group_id,
        ):
            row.status = status_value
            row.close_reason = reason
            row.closed_at = now
            row.updated_at = now
            closed += 1
    if closed:
        await session.flush()
    return closed


async def resolve_rollout_for_confirm(
    session: AsyncSession,
    *,
    project_id: uuid.UUID,
    version: str,
    target_filter: str,
    target_chip: str | None,
    group_id: uuid.UUID | None,
    policy_id: uuid.UUID | None,
    fallback_started_at: dt.datetime | None,
) -> tuple[ProjectRollout | None, bool, dt.datetime | None]:
    """Return `(row, is_closed, started_at)` for a confirm-driven rollout advancement."""
    if not isinstance(session, AsyncSession):
        return None, False, fallback_started_at

    stmt = (
        select(ProjectRollout)
        .where(
            ProjectRollout.project_id == project_id,
            ProjectRollout.version == version,
            ProjectRollout.target_filter == target_filter,
        )
        .order_by(ProjectRollout.created_at.desc(), ProjectRollout.id.desc())
    )
    stmt = (
        stmt.where(ProjectRollout.target_chip.is_(None))
        if target_chip is None
        else stmt.where(ProjectRollout.target_chip == target_chip)
    )
    stmt = (
        stmt.where(ProjectRollout.group_id.is_(None))
        if group_id is None
        else stmt.where(ProjectRollout.group_id == group_id)
    )
    if policy_id is not None:
        stmt = stmt.where(ProjectRollout.policy_id == policy_id)

    row = await session.scalar(stmt.limit(1))
    started_at = row.created_at if row is not None else fallback_started_at
    if row is not None and row.status != "open":
        return row, True, started_at

    # Also treat as superseded if a newer rollout of another version covers this scope.
    newer_stmt = (
        select(ProjectRollout)
        .where(
            ProjectRollout.project_id == project_id,
            ProjectRollout.version != version,
        )
        .order_by(ProjectRollout.created_at.desc(), ProjectRollout.id.desc())
    )
    if started_at is not None:
        newer_stmt = newer_stmt.where(ProjectRollout.created_at >= started_at)
    newer_rows = list((await session.scalars(newer_stmt)).all())
    for newer in newer_rows:
        if scope_covers(
            newer.target_filter,
            newer.target_chip,
            newer.group_id,
            target_filter,
            target_chip,
            group_id,
        ):
            if row is not None and row.status == "open":
                now = now_utc()
                row.status = "superseded"
                row.close_reason = f"superseded by rollout of {newer.version}"
                row.closed_at = now
                row.updated_at = now
                await session.commit()
            return row, True, started_at

    return row, False, started_at


async def reconcile_open_rollouts(
    session: AsyncSession,
    *,
    settings: Settings,
    publisher: CommandPublisher,
) -> list[DeployAccepted]:
    """Advance open cohort rollouts and soaking policies within the configured age bound (`A-21`)."""
    if (
        isinstance(publisher, NullCommandPublisher)
        or not settings.artifact_url_secret
        or not settings.public_base_url
    ):
        return []

    from fleetforge.deploy_policies import evaluate_policy_for_version
    from fleetforge.rollout import (
        advance_project_rollout_on_confirm,
        deploy_version_to_project_devices,
    )

    cutoff = now_utc() - dt.timedelta(seconds=max(60, int(settings.rollout_reconcile_max_age_s)))
    advanced_total: list[DeployAccepted] = []
    seen_policy_runs: set[tuple[uuid.UUID, str]] = set()

    open_rollouts = list(
        (
            await session.scalars(
                select(ProjectRollout)
                .where(ProjectRollout.status == "open", ProjectRollout.updated_at >= cutoff)
                .order_by(ProjectRollout.updated_at.desc(), ProjectRollout.id.desc())
                .limit(64)
            )
        ).all()
    )
    for row in open_rollouts:
        if row.policy_id is not None:
            pol = await session.get(DeployPolicy, row.policy_id)
            if pol is None or not pol.enabled:
                now = now_utc()
                row.status = "cancelled"
                row.close_reason = "policy disabled or deleted"
                row.closed_at = now
                row.updated_at = now
                await session.commit()
                continue
            seen_policy_runs.add((pol.id, row.version))
            ev = await evaluate_policy_for_version(
                session,
                policy=pol,
                version=row.version,
                settings=settings,
                publisher=publisher,
                admin=None,
                republish_in_flight=False,
            )
            if ev.status in ("completed", "halted"):
                now = now_utc()
                row.status = ev.status
                row.close_reason = ev.halt_reason
                row.closed_at = now
                row.updated_at = now
                await session.commit()
            advanced_total.extend(ev.deployed)
            continue

        deployed, _ = await deploy_version_to_project_devices(
            session,
            project_id=row.project_id,
            version=row.version,
            target_filter=row.target_filter,
            apply=row.apply,
            override=tuple(row.override),
            admin=None,
            settings=settings,
            publisher=publisher,
            target_chip=row.target_chip,
            group_id=row.group_id,
            best_effort_broker=True,
            canary=row.canary,
            max_parallel=row.max_parallel,
            sent_by_override=sender_from_json(row.sent_by),
            republish_in_flight=False,
            rollout_started_at=row.created_at,
        )
        advanced_total.extend(deployed)

    active_runs = list(
        (
            await session.execute(
                select(DeployPolicy, DeployPolicyRun)
                .join(DeployPolicyRun, DeployPolicyRun.policy_id == DeployPolicy.id)
                .where(
                    DeployPolicy.enabled.is_(True),
                    DeployPolicyRun.status == "active",
                    DeployPolicyRun.updated_at >= cutoff,
                )
                .order_by(DeployPolicyRun.updated_at.desc())
                .limit(64)
            )
        ).all()
    )
    for pol, run in active_runs:
        key = (pol.id, run.version)
        if key in seen_policy_runs:
            continue
        seen_policy_runs.add(key)
        ev = await evaluate_policy_for_version(
            session,
            policy=pol,
            version=run.version,
            settings=settings,
            publisher=publisher,
            admin=None,
            republish_in_flight=False,
        )
        advanced_total.extend(ev.deployed)

    legacy_rows = (
        await session.execute(_LEGACY_RECONCILE_CANDIDATES_SQL, {"cutoff": cutoff})
    ).all()
    for legacy in legacy_rows:
        advanced = await advance_project_rollout_on_confirm(
            session,
            device_id=str(legacy.device_id),
            cmd_id=str(legacy.cmd_id),
            settings=settings,
            publisher=publisher,
        )
        advanced_total.extend(advanced)

    return advanced_total

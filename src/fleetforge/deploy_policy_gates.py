"""Ring-halt gate evaluation and persisted `DeployPolicyRun` helpers (`A-22`)."""

from __future__ import annotations

import datetime as dt
import uuid

from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncSession

from fleetforge.db.models import DeployPolicy, DeployPolicyRun, Device
from fleetforge.deploys import DeploySnapshot
from fleetforge.faults import fault_counts, is_fault
from fleetforge.rollout_state import close_matching_rollouts
from fleetforge.rollout_types import PolicyRingStatus, RingStatusLiteral

_HISTORICAL_VERSION_HALT_SQL = text(
    """
    SELECT DISTINCT ON (device_id)
           device_id, state, at
      FROM deploy_events
     WHERE device_id = ANY(:device_ids)
       AND artifact_version = :version
       AND (
           state IN ('rolled_back', 'cancelled')
           OR (state = 'failed' AND COALESCE(detail ->> 'reason', '') <> 'publish_failed')
       )
     ORDER BY device_id, at DESC, id DESC
    """
)

_HISTORICAL_VERSION_HALT_SINCE_SQL = text(
    """
    SELECT DISTINCT ON (device_id)
           device_id, state, at
      FROM deploy_events
     WHERE device_id = ANY(:device_ids)
       AND artifact_version = :version
       AND at > :since
       AND (
           state IN ('rolled_back', 'cancelled')
           OR (state = 'failed' AND COALESCE(detail ->> 'reason', '') <> 'publish_failed')
       )
     ORDER BY device_id, at DESC, id DESC
    """
)


def device_confirmed_version(dev: Device, snap: DeploySnapshot | None, version: str) -> bool:
    if dev.fw_version == version:
        return True
    return snap is not None and snap.artifact_version == version and snap.state == "confirmed"


def device_in_flight_version(snap: DeploySnapshot | None, version: str) -> bool:
    return snap is not None and snap.artifact_version == version and not snap.is_terminal


def parse_iso_timestamp(raw: str | None) -> dt.datetime | None:
    if not isinstance(raw, str) or not raw:
        return None
    try:
        parsed = dt.datetime.fromisoformat(raw)
    except ValueError:
        return None
    return parsed if parsed.tzinfo is not None else parsed.replace(tzinfo=dt.UTC)


async def historical_version_halts(
    session: AsyncSession,
    *,
    device_ids: list[str],
    version: str,
    since: dt.datetime | None,
) -> dict[str, tuple[str, dt.datetime]]:
    if not device_ids or not isinstance(session, AsyncSession):
        return {}
    if since is None:
        rows = (
            await session.execute(
                _HISTORICAL_VERSION_HALT_SQL,
                {"device_ids": device_ids, "version": version},
            )
        ).all()
    else:
        rows = (
            await session.execute(
                _HISTORICAL_VERSION_HALT_SINCE_SQL,
                {"device_ids": device_ids, "version": version, "since": since},
            )
        ).all()
    return {str(row.device_id): (str(row.state), row.at) for row in rows}


async def detect_ring_halt(
    session: AsyncSession,
    *,
    ring_idx: int,
    ring_devices: list[Device],
    snapshots: dict[str, DeploySnapshot],
    version: str,
    min_free_heap_bytes: int | None,
    historical_halts: dict[str, tuple[str, dt.datetime]] | None = None,
    artifact_created_at: dt.datetime | None = None,
    resumed_at: dt.datetime | None = None,
) -> str | None:
    """Return a non-empty halt reason if any board in `ring_devices` tripped a safety/health gate."""
    hist = historical_halts or {}
    for dev in ring_devices:
        snap = snapshots.get(dev.device_id)
        if snap is not None and snap.artifact_version == version:
            if resumed_at is None or snap.at > resumed_at:
                if snap.state == "rolled_back":
                    return (
                        f"ring {ring_idx} halted: device {dev.device_id} rolled_back on "
                        f"version {version}"
                    )
                if snap.state == "cancelled":
                    return (
                        f"ring {ring_idx} halted: device {dev.device_id} cancelled on "
                        f"version {version}"
                    )
                if snap.state == "failed" and snap.reason != "publish_failed":
                    return (
                        f"ring {ring_idx} halted: device {dev.device_id} failed on "
                        f"version {version}"
                    )

        if dev.device_id in hist:
            hist_state, _ = hist[dev.device_id]
            return (
                f"ring {ring_idx} halted: device {dev.device_id} {hist_state} on version {version}"
            )

        if device_confirmed_version(dev, snap, version):
            if dev.safe_mode:
                return (
                    f"ring {ring_idx} halted: device {dev.device_id} entered safe_mode "
                    f"on version {version}"
                )
            if (
                min_free_heap_bytes is not None
                and dev.free_heap is not None
                and dev.free_heap < min_free_heap_bytes
            ):
                return (
                    f"ring {ring_idx} halted: device {dev.device_id} free_heap "
                    f"{dev.free_heap} < {min_free_heap_bytes} bytes"
                )
            fault_since: dt.datetime | None = None
            if snap is not None and snap.artifact_version == version:
                fault_since = snap.at
            elif artifact_created_at is not None:
                fault_since = artifact_created_at - dt.timedelta(seconds=1)
            if fault_since is not None:
                if resumed_at is not None and resumed_at > fault_since:
                    fault_since = resumed_at
                f_counts = await fault_counts(
                    session,
                    device_ids=[dev.device_id],
                    since=fault_since,
                )
                if sum(f_counts.get(dev.device_id, {}).values()) > 0:
                    return (
                        f"ring {ring_idx} halted: device {dev.device_id} recorded fault reset "
                        f"after confirming {version}"
                    )
            if (
                dev.fw_version == version
                and is_fault(dev.last_reset_reason)
                and (
                    resumed_at is None or (dev.last_seen is not None and dev.last_seen > resumed_at)
                )
            ):
                return (
                    f"ring {ring_idx} halted: device {dev.device_id} recorded fault reset "
                    f"({dev.last_reset_reason}) after confirming {version}"
                )
    return None


def pending_tail_rings(
    ring_ids: list[uuid.UUID],
    start_idx: int,
    by_group: dict[uuid.UUID, list[Device]],
    snapshots: dict[str, DeploySnapshot],
    version: str,
    *,
    count_confirmed: bool = False,
) -> list[PolicyRingStatus]:
    tail: list[PolicyRingStatus] = []
    for rem_idx, rem_gid in enumerate(ring_ids[start_idx:], start=start_idx):
        rem_devs = by_group.get(rem_gid, [])
        conf_n = (
            sum(
                1
                for d in rem_devs
                if device_confirmed_version(d, snapshots.get(d.device_id), version)
            )
            if count_confirmed
            else 0
        )
        tail.append(
            PolicyRingStatus(
                ring_index=rem_idx,
                group_id=rem_gid,
                status="pending",
                total_devices=len(rem_devs),
                confirmed_devices=conf_n,
                in_flight_devices=0,
            )
        )
    return tail


def build_halted_rings(
    ring_ids: list[uuid.UUID],
    by_group: dict[uuid.UUID, list[Device]],
    snapshots: dict[str, DeploySnapshot],
    version: str,
    halted_ring_idx: int,
) -> list[PolicyRingStatus]:
    halted_rings: list[PolicyRingStatus] = []
    for idx, gid in enumerate(ring_ids):
        r_devs = by_group.get(gid, [])
        confirmed_n = sum(
            1 for d in r_devs if device_confirmed_version(d, snapshots.get(d.device_id), version)
        )
        in_flight_n = sum(
            1 for d in r_devs if device_in_flight_version(snapshots.get(d.device_id), version)
        )
        r_status: RingStatusLiteral = (
            "halted"
            if idx == halted_ring_idx
            else ("promoted" if idx < halted_ring_idx and r_devs else "pending")
        )
        halted_rings.append(
            PolicyRingStatus(
                ring_index=idx,
                group_id=gid,
                status=r_status,
                total_devices=len(r_devs),
                confirmed_devices=confirmed_n,
                in_flight_devices=in_flight_n,
            )
        )
    return halted_rings


async def load_or_create_run(
    session: AsyncSession,
    *,
    policy_id: uuid.UUID,
    version: str,
    now: dt.datetime,
) -> DeployPolicyRun | None:
    if not isinstance(session, AsyncSession):
        return None
    run = await session.get(DeployPolicyRun, (policy_id, version))
    if run is None:
        run = DeployPolicyRun(
            policy_id=policy_id,
            version=version,
            status="active",
            ring_completed_at={},
            created_at=now,
            updated_at=now,
        )
        session.add(run)
        await session.flush()
    return run


async def persist_halt(
    session: AsyncSession,
    *,
    policy: DeployPolicy,
    run: DeployPolicyRun | None,
    version: str,
    ring_idx: int,
    reason: str,
    now: dt.datetime,
) -> None:
    if run is not None:
        run.status = "halted"
        run.halted_ring_index = ring_idx
        run.halt_reason = reason
        run.halted_at = now
        run.updated_at = now
    await close_matching_rollouts(
        session,
        project_id=policy.project_id,
        version=version,
        target_filter="all",
        target_chip=None,
        group_id=None,
        policy_id=policy.id,
        status_value="halted",
        reason=reason,
    )
    if isinstance(session, AsyncSession):
        await session.commit()

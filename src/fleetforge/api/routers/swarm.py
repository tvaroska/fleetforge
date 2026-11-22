"""Robotic Swarm Hierarchy (`V3-1`) and Binary Delta OTA (`V3-2`) routes.

Routes:
- `GET /v1/projects/{slug}/swarm` — inspect UGV parent -> child drone hierarchy, cached
  edge artifacts, and precomputed `ffdelta-v1` patches.
- `PUT /v1/devices/{gateway_id}/relay` — register/update a UGV gateway's `relay_base_url`
  and `cached_artifacts` set, returning signed upstream pull descriptors for child builds.
- `POST /v1/devices/{gateway_id}/relay-sync` — replay disconnected child `up/*` envelopes
  buffered by `gateway_id` while out of internet range (enforcing `child.parent_device_id == gateway_id`).
- `POST /v1/artifact/{to_sha256}/deltas` — compute and store an `ffdelta-v1` binary patch
  from `from_sha256` -> `to_sha256` in `ObjectStore`.
- `GET /v1/projects/{slug}/deltas` — list available `ffdelta-v1` patches for a project.
"""

from __future__ import annotations

import json
import logging
from typing import Annotated

from fastapi import APIRouter, Depends, HTTPException, Path, Query, status
from sqlalchemy import select, text

from fleetforge.api.deps import (
    AdminDep,
    ObjectStoreDep,
    SessionMakerDep,
    SettingsDep,
    authorize_artifact_digests,
    bearer_scheme,
    check_project_access,
    cookie_scheme,
    resolve_project_for_request,
)
from fleetforge.api.routers.deploys import _artifact_url
from fleetforge.api.schemas import PROJECT_SLUG_PATTERN
from fleetforge.api.schemas_v2 import (
    ArtifactDeltaList,
    ArtifactDeltaSummary,
    CreateDeltaRequest,
    GatewayArtifactPull,
    GatewayRelayConfigRequest,
    GatewayRelaySummary,
    GatewayRelaySyncRequest,
    GatewayRelaySyncResponse,
    SwarmChildNode,
    SwarmGatewayNode,
    SwarmTopologyResponse,
)
from fleetforge.clock import now_utc
from fleetforge.db.models import (
    Artifact,
    ArtifactDelta,
    Device,
    GatewayRelayState,
    ProjectRole,
)
from fleetforge.delta_ota import DELTA_FORMAT, DeltaPatchError, create_delta_patch
from fleetforge.events import emit
from fleetforge.ingestor.handlers import handle_up_message
from fleetforge.presence import is_online
from fleetforge.storage.blobs import SHA256_HEX, blob_key, digest_bytes, put_blob
from fleetforge.storage.objectstore import ObjectNotFound, ObjectStoreError
from fleetforge.swarm_gateway import validate_relay_base_url

logger = logging.getLogger(__name__)

router = APIRouter(
    prefix="/v1",
    tags=["swarm"],
    dependencies=[Depends(bearer_scheme), Depends(cookie_scheme)],
)

_DELTA_ARTIFACT_UPSERT_SQL = text(
    """
    INSERT INTO artifacts (sha256, size_bytes, kind, artifact_type)
    VALUES (:sha256, :size_bytes, 'delta_patch', 'delta_patch')
    ON CONFLICT (sha256) DO NOTHING
    """
)

_PROJECT_ARTIFACT_SHAS_SQL = text(
    """
    SELECT DISTINCT sha256
      FROM artifact_versions
     WHERE project_id = :project_id
    """
)


def _delta_summary(row: ArtifactDelta) -> ArtifactDeltaSummary:
    savings = (
        round((1.0 - (row.patch_size_bytes / row.target_size_bytes)) * 100.0, 1)
        if row.target_size_bytes > 0
        else 0.0
    )
    return ArtifactDeltaSummary(
        from_sha256=row.from_sha256,
        to_sha256=row.to_sha256,
        patch_sha256=row.patch_sha256,
        patch_size_bytes=row.patch_size_bytes,
        target_size_bytes=row.target_size_bytes,
        savings_pct=max(0.0, savings),
        format=row.format,
        created_at=row.created_at,
    )


@router.get(
    "/projects/{slug}/swarm",
    response_model=SwarmTopologyResponse,
    summary="Inspect parent UGV -> child drone hierarchy and binary delta patches",
)
async def get_project_swarm(
    slug: Annotated[str, Path(pattern=PROJECT_SLUG_PATTERN)],
    admin: AdminDep,
    sessionmaker: SessionMakerDep,
    settings: SettingsDep,
) -> SwarmTopologyResponse:
    now = now_utc()
    async with sessionmaker() as session:
        project = await resolve_project_for_request(
            session,
            admin,
            project_slug=slug,
            min_role=ProjectRole.VIEWER,
            required_scope="read",
        )
        devices = list(
            (
                await session.scalars(
                    select(Device)
                    .where(Device.project_id == project.id, Device.decommissioned_at.is_(None))
                    .order_by(Device.device_id)
                )
            ).all()
        )
        relays = (
            {
                r.gateway_id: r
                for r in (
                    await session.scalars(
                        select(GatewayRelayState).where(
                            GatewayRelayState.gateway_id.in_([d.device_id for d in devices])
                        )
                    )
                ).all()
            }
            if devices
            else {}
        )

        children_by_parent: dict[str, list[Device]] = {}
        for d in devices:
            if d.parent_device_id:
                children_by_parent.setdefault(d.parent_device_id, []).append(d)

        gateway_ids = sorted(
            {
                d.device_id
                for d in devices
                if d.device_id in children_by_parent
                or d.device_id in relays
                or "edge_relay" in (d.capabilities or [])
            }
        )
        by_id = {d.device_id: d for d in devices}
        gateways: list[SwarmGatewayNode] = []
        for gw_id in gateway_ids:
            gw_dev = by_id[gw_id]
            relay = relays.get(gw_id)
            child_nodes = [
                SwarmChildNode(
                    device_id=c.device_id,
                    name=c.name,
                    platform_type=c.platform_type,
                    fw_version=c.fw_version,
                    link_type=c.link_type,
                    online=is_online(c, now=now, tolerance=settings.presence_tolerance),
                    has_delta_capability=("delta" in (c.capabilities or [])),
                )
                for c in children_by_parent.get(gw_id, [])
            ]
            gateways.append(
                SwarmGatewayNode(
                    gateway_id=gw_id,
                    name=gw_dev.name,
                    platform_type=gw_dev.platform_type,
                    fw_version=gw_dev.fw_version,
                    online=is_online(gw_dev, now=now, tolerance=settings.presence_tolerance),
                    relay_base_url=relay.relay_base_url if relay else None,
                    cached_artifacts=list(relay.cached_artifacts) if relay else [],
                    children=child_nodes,
                )
            )

        direct_count = sum(
            1 for d in devices if not d.parent_device_id and d.device_id not in set(gateway_ids)
        )

        proj_shas = [
            str(r[0])
            for r in (
                await session.execute(_PROJECT_ARTIFACT_SHAS_SQL, {"project_id": project.id})
            ).all()
        ]
        delta_rows = (
            list(
                (
                    await session.scalars(
                        select(ArtifactDelta)
                        .where(ArtifactDelta.to_sha256.in_(proj_shas))
                        .order_by(ArtifactDelta.created_at.desc())
                    )
                ).all()
            )
            if proj_shas
            else []
        )

    return SwarmTopologyResponse(
        project=project.slug,
        gateways=gateways,
        direct_device_count=direct_count,
        deltas=[_delta_summary(r) for r in delta_rows],
    )


@router.put(
    "/devices/{gateway_id}/relay",
    response_model=GatewayRelaySummary,
    summary="Register or update a UGV gateway's edge artifact cache and list upstream pulls",
)
async def upsert_gateway_relay(
    gateway_id: str,
    body: GatewayRelayConfigRequest,
    admin: AdminDep,
    sessionmaker: SessionMakerDep,
    settings: SettingsDep,
) -> GatewayRelaySummary:
    try:
        clean_url = validate_relay_base_url(body.relay_base_url)
    except ValueError as exc:
        raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail=str(exc)) from None

    clean_cached: list[str] = []
    for sha in body.cached_artifacts:
        if not SHA256_HEX.match(sha):
            raise HTTPException(
                status_code=status.HTTP_400_BAD_REQUEST,
                detail=f"invalid sha256 in cached_artifacts: {sha!r}",
            )
        if sha not in clean_cached:
            clean_cached.append(sha)

    async with sessionmaker() as session:
        gateway = await session.get(Device, gateway_id)
        if gateway is None or gateway.decommissioned_at is not None:
            raise HTTPException(
                status_code=status.HTTP_404_NOT_FOUND, detail=f"no such device: {gateway_id}"
            )
        await check_project_access(
            session,
            admin,
            gateway.project_id,
            min_role=ProjectRole.OPERATOR,
            required_scope="deploy",
            not_found=HTTPException(
                status_code=status.HTTP_404_NOT_FOUND, detail=f"no such device: {gateway_id}"
            ),
        )
        now = now_utc()
        relay = await session.get(GatewayRelayState, gateway_id)
        if relay is None:
            relay = GatewayRelayState(
                gateway_id=gateway_id,
                relay_base_url=clean_url,
                cached_artifacts=clean_cached,
                updated_at=now,
            )
            session.add(relay)
        else:
            relay.relay_base_url = clean_url
            relay.cached_artifacts = clean_cached
            relay.updated_at = now

        children = list(
            (
                await session.scalars(
                    select(Device)
                    .where(
                        Device.project_id == gateway.project_id,
                        Device.parent_device_id == gateway_id,
                        Device.decommissioned_at.is_(None),
                    )
                    .order_by(Device.device_id)
                )
            ).all()
        )
        child_targets = {c.platform_type for c in children}
        needed_pulls: list[GatewayArtifactPull] = []
        if child_targets and settings.artifact_url_secret and settings.public_base_url:
            art_rows = (
                await session.execute(
                    text(
                        """
                        SELECT DISTINCT a.sha256, a.size_bytes
                          FROM artifact_versions AS v
                          JOIN artifacts AS a ON a.sha256 = v.sha256
                         WHERE v.project_id = :pid AND v.target = ANY(:targets)
                        """
                    ),
                    {"pid": gateway.project_id, "targets": list(child_targets)},
                )
            ).all()
            cached_set = set(clean_cached)
            for row in art_rows:
                sha_str = str(row.sha256)
                if sha_str not in cached_set:
                    needed_pulls.append(
                        GatewayArtifactPull(
                            sha256=sha_str,
                            size_bytes=int(row.size_bytes),
                            url=_artifact_url(sha_str, settings),
                        )
                    )

        await session.commit()
        await session.refresh(relay)

    return GatewayRelaySummary(
        gateway_id=gateway_id,
        relay_base_url=relay.relay_base_url,
        cached_artifacts=list(relay.cached_artifacts),
        child_device_ids=[c.device_id for c in children],
        needed_artifacts=needed_pulls,
        updated_at=relay.updated_at,
    )


@router.post(
    "/devices/{gateway_id}/relay-sync",
    response_model=GatewayRelaySyncResponse,
    summary="Replay offline child `up/*` envelopes buffered by a UGV edge-relay gateway",
)
async def sync_gateway_relay(
    gateway_id: str,
    body: GatewayRelaySyncRequest,
    admin: AdminDep,
    sessionmaker: SessionMakerDep,
    settings: SettingsDep,
) -> GatewayRelaySyncResponse:
    now = now_utc()
    statuses_recorded = 0
    telemetry_recorded = 0
    logs_recorded = 0
    synced_children: list[str] = []

    async with sessionmaker() as session:
        gateway = await session.get(Device, gateway_id)
        if gateway is None or gateway.decommissioned_at is not None:
            raise HTTPException(
                status_code=status.HTTP_404_NOT_FOUND, detail=f"no such device: {gateway_id}"
            )
        await check_project_access(
            session,
            admin,
            gateway.project_id,
            min_role=ProjectRole.OPERATOR,
            required_scope="deploy",
            not_found=HTTPException(
                status_code=status.HTTP_404_NOT_FOUND, detail=f"no such device: {gateway_id}"
            ),
        )

        # Validate every child belongs to `gateway_id` BEFORE replaying any envelope.
        for child_id in body.children:
            child = await session.get(Device, child_id)
            if (
                child is None
                or child.decommissioned_at is not None
                or child.project_id != gateway.project_id
            ):
                raise HTTPException(
                    status_code=status.HTTP_404_NOT_FOUND,
                    detail=f"unknown child device: {child_id}",
                )
            if child.parent_device_id != gateway_id:
                raise HTTPException(
                    status_code=status.HTTP_403_FORBIDDEN,
                    detail=f"device {child_id} is not attached to parent gateway {gateway_id}",
                )

        gateway.last_seen = now
        for child_id, bundle in body.children.items():
            synced_children.append(child_id)
            if bundle.announce is not None:
                ev = await handle_up_message(
                    session,
                    topic=f"ff/v1/d/{child_id}/up/announce",
                    payload=json.dumps(bundle.announce, separators=(",", ":")).encode("utf-8"),
                    retained=False,
                    received_at=now,
                    tolerance=settings.presence_tolerance,
                )
                if ev is not None:
                    await emit(session, ev)
            for st_item in bundle.statuses:
                ev = await handle_up_message(
                    session,
                    topic=f"ff/v1/d/{child_id}/up/status",
                    payload=json.dumps(st_item, separators=(",", ":")).encode("utf-8"),
                    retained=False,
                    received_at=now,
                    tolerance=settings.presence_tolerance,
                )
                if ev is not None:
                    statuses_recorded += 1
                    await emit(session, ev)
            for tel_item in bundle.telemetry:
                ev = await handle_up_message(
                    session,
                    topic=f"ff/v1/d/{child_id}/up/telemetry",
                    payload=json.dumps(tel_item, separators=(",", ":")).encode("utf-8"),
                    retained=False,
                    received_at=now,
                    tolerance=settings.presence_tolerance,
                )
                if ev is not None:
                    telemetry_recorded += 1
                    await emit(session, ev)
            for log_item in bundle.logs:
                ev = await handle_up_message(
                    session,
                    topic=f"ff/v1/d/{child_id}/up/log",
                    payload=json.dumps(log_item, separators=(",", ":")).encode("utf-8"),
                    retained=False,
                    received_at=now,
                    tolerance=settings.presence_tolerance,
                )
                if ev is not None:
                    logs_recorded += 1
                    await emit(session, ev)

        relay = await session.get(GatewayRelayState, gateway_id)
        if body.cached_artifacts is not None:
            clean_cached = [s for s in body.cached_artifacts if SHA256_HEX.match(s)]
            if relay is not None:
                relay.cached_artifacts = clean_cached
                relay.updated_at = now
        final_cached = list(relay.cached_artifacts) if relay is not None else []
        await session.commit()

    return GatewayRelaySyncResponse(
        gateway_id=gateway_id,
        synced_children=synced_children,
        statuses_recorded=statuses_recorded,
        telemetry_batches_recorded=telemetry_recorded,
        log_batches_recorded=logs_recorded,
        cached_artifacts=final_cached,
    )


@router.post(
    "/artifact/{to_sha256}/deltas",
    response_model=ArtifactDeltaSummary,
    status_code=status.HTTP_201_CREATED,
    summary="Compute and store an `ffdelta-v1` binary delta patch between two artifacts",
)
async def create_artifact_delta(
    to_sha256: str,
    body: CreateDeltaRequest,
    admin: AdminDep,
    store: ObjectStoreDep,
    sessionmaker: SessionMakerDep,
    project: Annotated[
        str | None,
        Query(pattern=PROJECT_SLUG_PATTERN, description="Optional project scope check"),
    ] = None,
) -> ArtifactDeltaSummary:
    if not SHA256_HEX.match(to_sha256):
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail="to_sha256 must be 64 lowercase hex characters",
        )
    if body.from_sha256 == to_sha256:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail="from_sha256 and to_sha256 must be distinct firmware digests",
        )

    async with sessionmaker() as session:
        await authorize_artifact_digests(
            session,
            admin,
            (body.from_sha256, to_sha256),
            project_slug=project,
            min_role=ProjectRole.OPERATOR,
            required_scope=("write:artifacts", "deploy"),
        )

        existing = await session.scalar(
            select(ArtifactDelta).where(
                ArtifactDelta.from_sha256 == body.from_sha256,
                ArtifactDelta.to_sha256 == to_sha256,
            )
        )
        if existing is not None:
            return _delta_summary(existing)

        from_art = await session.get(Artifact, body.from_sha256)
        to_art = await session.get(Artifact, to_sha256)
        if from_art is None or to_art is None:
            raise HTTPException(
                status_code=status.HTTP_404_NOT_FOUND,
                detail="base or target artifact not found",
            )

    try:
        base_bytes = await store.get(blob_key(body.from_sha256))
        target_bytes = await store.get(blob_key(to_sha256))
    except ObjectNotFound:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail="base or target artifact bytes missing from object store",
        ) from None
    except ObjectStoreError:
        raise HTTPException(
            status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
            detail="the artifact store cannot be reached; try again shortly",
        ) from None

    try:
        patch_bytes = create_delta_patch(base_bytes, target_bytes)
    except DeltaPatchError as exc:
        raise HTTPException(
            status_code=status.HTTP_422_UNPROCESSABLE_CONTENT, detail=str(exc)
        ) from None

    patch_sha = digest_bytes(patch_bytes)
    try:
        await put_blob(store, patch_bytes)
    except ObjectStoreError:
        raise HTTPException(
            status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
            detail="the artifact store cannot be reached; try again shortly",
        ) from None

    async with sessionmaker() as session:
        await session.execute(
            _DELTA_ARTIFACT_UPSERT_SQL,
            {"sha256": patch_sha, "size_bytes": len(patch_bytes)},
        )
        row = ArtifactDelta(
            from_sha256=body.from_sha256,
            to_sha256=to_sha256,
            patch_sha256=patch_sha,
            patch_size_bytes=len(patch_bytes),
            target_size_bytes=len(target_bytes),
            format=DELTA_FORMAT,
        )
        session.add(row)
        await session.commit()
        await session.refresh(row)
        return _delta_summary(row)


@router.get(
    "/projects/{slug}/deltas",
    response_model=ArtifactDeltaList,
    summary="List precomputed binary delta patches for a project",
)
async def list_project_deltas(
    slug: Annotated[str, Path(pattern=PROJECT_SLUG_PATTERN)],
    admin: AdminDep,
    sessionmaker: SessionMakerDep,
) -> ArtifactDeltaList:
    async with sessionmaker() as session:
        project = await resolve_project_for_request(
            session,
            admin,
            project_slug=slug,
            min_role=ProjectRole.VIEWER,
            required_scope="read",
        )
        proj_shas = [
            str(r[0])
            for r in (
                await session.execute(_PROJECT_ARTIFACT_SHAS_SQL, {"project_id": project.id})
            ).all()
        ]
        rows = (
            list(
                (
                    await session.scalars(
                        select(ArtifactDelta)
                        .where(ArtifactDelta.to_sha256.in_(proj_shas))
                        .order_by(ArtifactDelta.created_at.desc())
                    )
                ).all()
            )
            if proj_shas
            else []
        )
    return ArtifactDeltaList(deltas=[_delta_summary(r) for r in rows])

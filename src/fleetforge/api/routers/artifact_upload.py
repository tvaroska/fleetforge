"""`POST /v1/artifact` — validate, sign, store, simulate, and auto-deploy firmware (`R1-be-1`, `A-11`).

Kept on its own admin-authenticated `APIRouter(prefix="/v1/artifact")` alongside `artifacts.py`
(`GET /v1/artifact`) and `artifact_sim.py`, separate from the public signature-authorized
`artifact_download.py` router.
"""

from __future__ import annotations

import logging
import uuid
from dataclasses import dataclass
from typing import Annotated, Any

from fastapi import APIRouter, Depends, HTTPException, Query, Request, Response, status
from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncSession

from fleetforge.api.deps import (
    AdminDep,
    AuthContext,
    CommandPublisherDep,
    ObjectStoreDep,
    SessionMakerDep,
    SettingsDep,
    SimRunnerDep,
    bearer_scheme,
    cookie_scheme,
    resolve_project_for_request,
)
from fleetforge.api.schemas import PROJECT_SLUG_PATTERN, ArtifactUploaded
from fleetforge.db.models import ArtifactKind, Project, ProjectRole
from fleetforge.deploy_policies import trigger_matching_policies_on_upload
from fleetforge.deploy_precheck import merged_binary
from fleetforge.firmware.manifest import EXPECTED_PARTITION_LAYOUT, SafeSegment
from fleetforge.lib_marker import LibMarker, find_marker
from fleetforge.merged_image import detect_merged
from fleetforge.partition_profiles import (
    MAX_PROFILE_SLOT_SIZE,
    LayoutCatalog,
    load_layout_catalog,
)
from fleetforge.rollout import deploy_version_to_project_devices, matches_project_tracking
from fleetforge.rollout_types import VERSION_PATTERN
from fleetforge.signing import SigningKeyRing, is_valid_sig_format
from fleetforge.storage.blobs import digest_bytes, put_blob
from fleetforge.storage.objectstore import ObjectStoreError
from fleetforge.url_safety import validate_http_link_url

logger = logging.getLogger(__name__)

router = APIRouter(
    prefix="/v1/artifact",
    tags=["artifacts"],
    dependencies=[Depends(bearer_scheme), Depends(cookie_scheme)],
)

CHUNK_SIZE = 64 * 1024
STORE_UNAVAILABLE = "the artifact store cannot be reached; try again shortly"

_ARTIFACT_UPSERT_SQL = text(
    """
    INSERT INTO artifacts (
        sha256, size_bytes, kind, artifact_type, target, partition_layout, has_lib_marker, sig,
        sim_status
    )
    VALUES (
        :sha256, :size_bytes, :kind, :artifact_type, :target, :partition_layout,
        :has_lib_marker, :sig, :sim_status
    )
    ON CONFLICT (sha256) DO UPDATE
       SET has_lib_marker = COALESCE(artifacts.has_lib_marker, EXCLUDED.has_lib_marker),
           sig = COALESCE(artifacts.sig, EXCLUDED.sig),
           sim_status = COALESCE(artifacts.sim_status, EXCLUDED.sim_status)
     WHERE artifacts.has_lib_marker IS NULL
        OR (artifacts.sig IS NULL AND EXCLUDED.sig IS NOT NULL)
        OR (artifacts.sim_status IS NULL AND EXCLUDED.sim_status IS NOT NULL)
    """
)

_LABEL_INSERT_SQL = text(
    """
    INSERT INTO artifact_versions (
        project_id, target, version, sha256,
        repo_url, branch, commit_sha, tag, build_url, test_passed
    )
    VALUES (
        :project_id, :target, :version, :sha256,
        :repo_url, :branch, :commit_sha, :tag, :build_url, :test_passed
    )
    ON CONFLICT (project_id, target, version) DO NOTHING
    RETURNING sha256
    """
)

_LABEL_PROVENANCE_UPDATE_SQL = text(
    """
    UPDATE artifact_versions
       SET repo_url = COALESCE(:repo_url, repo_url),
           branch = COALESCE(:branch, branch),
           commit_sha = COALESCE(:commit_sha, commit_sha),
           tag = COALESCE(:tag, tag),
           build_url = COALESCE(:build_url, build_url),
           test_passed = COALESCE(:test_passed, test_passed)
     WHERE project_id = :project_id AND target = :target AND version = :version
    """
)

_LABEL_SELECT_SQL = text(
    "SELECT sha256 FROM artifact_versions "
    "WHERE project_id = :project_id AND target = :target AND version = :version"
)

_ARTIFACT_SELECT_SQL = text(
    "SELECT sha256, size_bytes, target, partition_layout, artifact_type, has_lib_marker, sig, "
    "sim_status, sim_detail, sim_runner, sim_ran_at "
    "FROM artifacts WHERE sha256 = :sha256"
)

_ARTIFACT_SIM_UPDATE_SQL = text(
    """
    UPDATE artifacts
       SET sim_status = :sim_status,
           sim_detail = :sim_detail,
           sim_runner = :sim_runner,
           sim_ran_at = :sim_ran_at
     WHERE sha256 = :sha256
    """
)


@dataclass(frozen=True)
class _TargetQuery:
    target: SafeSegment
    version: str
    partition_layout: SafeSegment
    artifact_type: SafeSegment
    project: str | None


@dataclass(frozen=True)
class _ProvenanceQuery:
    repo_url: str | None
    branch: str | None
    commit_sha: str | None
    tag: str | None
    build_url: str | None


@dataclass(frozen=True)
class _OptionsQuery:
    test_passed: bool | None
    sig: str | None
    simulate: bool
    queue_simulation: bool


@dataclass(frozen=True)
class _UploadQuery:
    target_q: _TargetQuery
    provenance: _ProvenanceQuery
    options: _OptionsQuery


@dataclass(frozen=True)
class _UploadServices:
    store: ObjectStoreDep
    sessionmaker: SessionMakerDep
    settings: SettingsDep
    publisher: CommandPublisherDep
    sim_runner: SimRunnerDep


@dataclass(frozen=True)
class _PreparedPayload:
    data: bytes
    digest: str
    stored_layout: str | None
    marker: LibMarker | None
    has_lib_marker: bool | None
    verified_sig: str | None
    repo_url: str | None
    build_url: str | None


def _parse_target_query(
    target: Annotated[SafeSegment, Query(description="Chip target, e.g. esp32s3")],
    version: Annotated[str, Query(description="The version label, e.g. 1.5.0")],
    partition_layout: Annotated[
        SafeSegment, Query(description="Partition layout the image is built for")
    ] = EXPECTED_PARTITION_LAYOUT,
    artifact_type: Annotated[
        SafeSegment,
        Query(
            description=(
                "Artifact cargo type (app, mcu_fw, model_weights, fpga_bitstream, data_partition)"
            )
        ),
    ] = "app",
    project: Annotated[
        str | None,
        Query(pattern=PROJECT_SLUG_PATTERN, description="Project slug (defaults to token/default)"),
    ] = None,
) -> _TargetQuery:
    return _TargetQuery(
        target=target,
        version=version,
        partition_layout=partition_layout,
        artifact_type=artifact_type,
        project=project,
    )


def _parse_provenance_query(
    repo_url: Annotated[
        str | None,
        Query(max_length=512, description="Optional Git repository URL"),
    ] = None,
    branch: Annotated[
        str | None,
        Query(max_length=128, description="Optional Git branch name"),
    ] = None,
    commit_sha: Annotated[
        str | None,
        Query(max_length=64, description="Optional Git commit SHA"),
    ] = None,
    tag: Annotated[
        str | None,
        Query(max_length=128, description="Optional Git tag"),
    ] = None,
    build_url: Annotated[
        str | None,
        Query(max_length=512, description="Optional CI build URL"),
    ] = None,
) -> _ProvenanceQuery:
    return _ProvenanceQuery(
        repo_url=repo_url,
        branch=branch,
        commit_sha=commit_sha,
        tag=tag,
        build_url=build_url,
    )


def _parse_options_query(
    test_passed: Annotated[
        bool | None,
        Query(description="Optional CI test / QEMU verification outcome"),
    ] = None,
    sig: Annotated[
        str | None,
        Query(
            max_length=160,
            description=(
                "Optional detached ECDSA P-256 signature (ecdsa-p256-sha256:<base64>) "
                "verified against the public key rotation ring"
            ),
        ),
    ] = None,
    simulate: Annotated[
        bool,
        Query(description="Run pre-flight R9 simulation immediately on upload"),
    ] = False,
    queue_simulation: Annotated[
        bool,
        Query(description="Queue pre-flight R9 simulation (`sim_status='pending'`) for sim_worker"),
    ] = False,
) -> _OptionsQuery:
    return _OptionsQuery(
        test_passed=test_passed,
        sig=sig,
        simulate=simulate,
        queue_simulation=queue_simulation,
    )


def _parse_upload_query(
    target_q: Annotated[_TargetQuery, Depends(_parse_target_query)],
    provenance: Annotated[_ProvenanceQuery, Depends(_parse_provenance_query)],
    options: Annotated[_OptionsQuery, Depends(_parse_options_query)],
) -> _UploadQuery:
    return _UploadQuery(target_q=target_q, provenance=provenance, options=options)


def _upload_services(
    store: ObjectStoreDep,
    sessionmaker: SessionMakerDep,
    settings: SettingsDep,
    publisher: CommandPublisherDep,
    sim_runner: SimRunnerDep,
) -> _UploadServices:
    return _UploadServices(
        store=store,
        sessionmaker=sessionmaker,
        settings=settings,
        publisher=publisher,
        sim_runner=sim_runner,
    )


def _validate_version_and_links(query: _UploadQuery) -> tuple[str | None, str | None]:
    if not VERSION_PATTERN.match(query.target_q.version):
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail=(
                "version must be 1-64 characters of letters, digits, '.', '_', '+' or "
                "'-', starting with a letter or digit"
            ),
        )
    repo_url = query.provenance.repo_url
    build_url = query.provenance.build_url
    try:
        if repo_url is not None:
            repo_url = validate_http_link_url(repo_url, field_name="repo_url")
        if build_url is not None:
            build_url = validate_http_link_url(build_url, field_name="build_url")
    except ValueError as exc:
        raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail=str(exc)) from None
    return repo_url, build_url


def _resolve_slot_limit(
    request: Request,
    target_q: _TargetQuery,
    catalog: LayoutCatalog,
) -> tuple[int, str | None]:
    if target_q.artifact_type == "app":
        try:
            limit = catalog.layouts[target_q.partition_layout].ota_slot_size
        except KeyError:
            known = ", ".join(sorted(catalog.layouts))
            raise HTTPException(
                status_code=status.HTTP_400_BAD_REQUEST,
                detail=f"unknown partition_layout; this server understands: {known}",
            ) from None
        return limit, target_q.partition_layout
    stored_layout = (
        target_q.partition_layout
        if (
            "partition_layout" in request.query_params
            and target_q.partition_layout in catalog.layouts
        )
        else None
    )
    return MAX_PROFILE_SLOT_SIZE, stored_layout


def _too_large(size: int, limit: int) -> HTTPException:
    return HTTPException(
        status_code=status.HTTP_413_CONTENT_TOO_LARGE,
        detail=(
            f"artifact is {size} bytes; an OTA slot in this partition layout is {limit}. "
            "The image must fit the slot it will be written into. "
            "A merged full-flash image (bootloader, partition table and app) is bigger than "
            "its app: upload the app .bin."
        ),
    )


def _declared_length(request: Request, limit: int) -> int:
    raw = request.headers.get("content-length")
    if raw is None:
        raise HTTPException(
            status_code=status.HTTP_411_LENGTH_REQUIRED,
            detail="Content-Length is required",
        )
    try:
        declared = int(raw)
    except ValueError:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST, detail="Content-Length is not a number"
        ) from None
    if declared > limit:
        raise _too_large(declared, limit)
    return declared


async def _read_validated_body(
    request: Request,
    limit: int,
    target_q: _TargetQuery,
    token_id: uuid.UUID,
) -> bytes:
    _declared_length(request, limit)
    chunks: list[bytes] = []
    total = 0
    async for chunk in request.stream():
        total += len(chunk)
        if total > limit:
            raise _too_large(total, limit)
        chunks.append(chunk)
    data = b"".join(chunks)
    if not data:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail="artifact is empty; a deploy that ships nothing is never intended",
        )
    if target_q.artifact_type == "app":
        merged = detect_merged(data)
        if merged is not None:
            finding = merged_binary(merged)
            logger.info(
                "artifact upload refused: %s (%s/%s, %d bytes) by %s",
                finding.code,
                target_q.target,
                target_q.version,
                len(data),
                token_id,
            )
            raise HTTPException(
                status_code=status.HTTP_422_UNPROCESSABLE_CONTENT, detail=finding.message
            )
    return data


def _verify_or_sign_digest(digest: str, sig: str | None, settings: SettingsDep) -> str | None:
    try:
        key_ring = SigningKeyRing.from_config(
            private_key=settings.artifact_signing_key,
            public_keys=settings.artifact_signing_public_keys,
        )
    except ValueError as exc:
        logger.error("artifact signing key configuration is invalid: %s", exc)
        raise HTTPException(
            status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
            detail="server signing key configuration is invalid",
        ) from None
    if sig is not None:
        if not is_valid_sig_format(sig):
            raise HTTPException(
                status_code=status.HTTP_400_BAD_REQUEST,
                detail="sig must be ecdsa-p256-sha256:<base64_der_signature>",
            )
        if not key_ring.verify_digest(digest, sig):
            raise HTTPException(
                status_code=status.HTTP_422_UNPROCESSABLE_CONTENT,
                detail="artifact signature verification failed against the trusted public key ring",
            )
        return sig
    return key_ring.sign_digest(digest) if key_ring.can_sign else None


def _version_conflict(target: str, version: str, existing: str | None) -> HTTPException:
    return HTTPException(
        status_code=status.HTTP_409_CONFLICT,
        detail=(
            f"{target} version {version} already names artifact {existing}. "
            "A version label is not re-pointed; upload the new image under a new version."
        ),
    )


def _check_blob_metadata_match(
    digest: str,
    stored_row: Any,
    target_q: _TargetQuery,
    stored_layout: str | None,
) -> HTTPException | None:
    if (
        stored_row.target == target_q.target
        and stored_row.partition_layout == stored_layout
        and stored_row.artifact_type == target_q.artifact_type
    ):
        return None
    return HTTPException(
        status_code=status.HTTP_409_CONFLICT,
        detail=(
            f"artifact {digest[:12]} is already stored with "
            f"(target={stored_row.target!r}, partition_layout={stored_row.partition_layout!r}, "
            f"artifact_type={stored_row.artifact_type!r}); cannot re-upload the same bytes with "
            f"(target={target_q.target!r}, partition_layout={stored_layout!r}, "
            f"artifact_type={target_q.artifact_type!r})"
        ),
    )


async def _upsert_blob_and_label(
    session: AsyncSession,
    project_id: uuid.UUID,
    query: _UploadQuery,
    payload: _PreparedPayload,
    services: _UploadServices,
) -> tuple[Any, bool]:
    tq = query.target_q
    existing = await session.scalar(
        _LABEL_SELECT_SQL,
        {"project_id": project_id, "target": tq.target, "version": tq.version},
    )
    if existing is not None and existing != payload.digest:
        raise _version_conflict(tq.target, tq.version, existing)

    existing_blob = (
        await session.execute(_ARTIFACT_SELECT_SQL, {"sha256": payload.digest})
    ).first()
    if existing_blob is not None:
        conflict = _check_blob_metadata_match(
            payload.digest, existing_blob, tq, payload.stored_layout
        )
        if conflict is not None:
            raise conflict

    await session.execute(
        text("SELECT pg_advisory_xact_lock(hashtext(:sha256))"),
        {"sha256": payload.digest},
    )
    try:
        await put_blob(services.store, payload.data)
    except ObjectStoreError:
        logger.exception("artifact upload failed: object store unavailable")
        raise HTTPException(
            status_code=status.HTTP_503_SERVICE_UNAVAILABLE, detail=STORE_UNAVAILABLE
        ) from None

    initial_sim_status = (
        "pending"
        if (
            not query.options.simulate
            and tq.artifact_type == "app"
            and (query.options.queue_simulation or services.settings.auto_queue_simulation)
        )
        else None
    )
    await session.execute(
        _ARTIFACT_UPSERT_SQL,
        {
            "sha256": payload.digest,
            "size_bytes": len(payload.data),
            "kind": ArtifactKind.USER_FIRMWARE.value,
            "artifact_type": tq.artifact_type,
            "target": tq.target,
            "partition_layout": payload.stored_layout,
            "has_lib_marker": payload.has_lib_marker,
            "sig": payload.verified_sig,
            "sim_status": initial_sim_status,
        },
    )
    stored_blob = (await session.execute(_ARTIFACT_SELECT_SQL, {"sha256": payload.digest})).one()
    conflict = _check_blob_metadata_match(payload.digest, stored_blob, tq, payload.stored_layout)
    if conflict is not None:
        await session.rollback()
        raise conflict

    created = await _write_version_label(session, project_id, query, payload)
    stored_blob = await _maybe_run_inline_simulation(session, query, payload, services, stored_blob)
    await session.commit()
    return stored_blob, created


async def _write_version_label(
    session: AsyncSession,
    project_id: uuid.UUID,
    query: _UploadQuery,
    payload: _PreparedPayload,
) -> bool:
    tq = query.target_q
    params = {
        "project_id": project_id,
        "target": tq.target,
        "version": tq.version,
        "sha256": payload.digest,
        "repo_url": payload.repo_url,
        "branch": query.provenance.branch,
        "commit_sha": query.provenance.commit_sha,
        "tag": query.provenance.tag,
        "build_url": payload.build_url,
        "test_passed": query.options.test_passed,
    }
    labelled = await session.scalar(_LABEL_INSERT_SQL, params)
    if labelled is not None:
        return True
    current = await session.scalar(
        _LABEL_SELECT_SQL,
        {"project_id": project_id, "target": tq.target, "version": tq.version},
    )
    if current != payload.digest:
        await session.rollback()
        raise _version_conflict(tq.target, tq.version, current)
    await session.execute(_LABEL_PROVENANCE_UPDATE_SQL, params)
    return False


async def _maybe_run_inline_simulation(
    session: AsyncSession,
    query: _UploadQuery,
    payload: _PreparedPayload,
    services: _UploadServices,
    stored_blob: Any,
) -> Any:
    if not query.options.simulate or services.sim_runner is None:
        return stored_blob
    sim_res = await services.sim_runner.simulate(
        sha256=payload.digest,
        target=query.target_q.target,
        image=payload.data,
        partition_layout=payload.stored_layout,
        artifact_type=query.target_q.artifact_type,
    )
    await session.execute(
        _ARTIFACT_SIM_UPDATE_SQL,
        {
            "sha256": payload.digest,
            "sim_status": sim_res.status.value,
            "sim_detail": sim_res.detail,
            "sim_runner": sim_res.runner,
            "sim_ran_at": sim_res.ran_at,
        },
    )
    return (await session.execute(_ARTIFACT_SELECT_SQL, {"sha256": payload.digest})).one()


async def _trigger_post_upload_deploys(
    session: AsyncSession,
    target_project: Project,
    admin: AuthContext,
    query: _UploadQuery,
    services: _UploadServices,
) -> list[str] | None:
    if "admin" not in admin.scopes and "deploy" not in admin.scopes:
        return None
    repo_url, _ = _validate_version_and_links(query)
    tracked = matches_project_tracking(
        target_project,
        repo_url=repo_url,
        branch=query.provenance.branch,
        commit_sha=query.provenance.commit_sha,
        tag=query.provenance.tag,
        test_passed=query.options.test_passed,
    )
    collected_ids: list[str] = []
    if tracked:
        deployed_list, _ = await deploy_version_to_project_devices(
            session,
            project_id=target_project.id,
            version=query.target_q.version,
            target_filter=target_project.auto_deploy_target,
            apply="auto",
            override=(),
            admin=admin,
            settings=services.settings,
            publisher=services.publisher,
            target_chip=query.target_q.target,
            best_effort_broker=True,
        )
        collected_ids.extend(d.device_id for d in deployed_list)

    sim_row = (
        await session.execute(
            _LABEL_SELECT_SQL,
            {
                "project_id": target_project.id,
                "target": query.target_q.target,
                "version": query.target_q.version,
            },
        )
    ).scalar_one()
    stored_blob = (await session.execute(_ARTIFACT_SELECT_SQL, {"sha256": sim_row})).one()
    policy_ids = await trigger_matching_policies_on_upload(
        session,
        project_id=target_project.id,
        version=query.target_q.version,
        branch=query.provenance.branch,
        tag=query.provenance.tag,
        test_passed=query.options.test_passed,
        sim_status=stored_blob.sim_status,
        admin=admin,
        settings=services.settings,
        publisher=services.publisher,
    )
    for pid in policy_ids:
        if pid not in collected_ids:
            collected_ids.append(pid)
    return collected_ids if (collected_ids or tracked) else None


def _log_upload_outcome(
    payload: _PreparedPayload,
    target_q: _TargetQuery,
    created: bool,
    token_id: uuid.UUID,
) -> None:
    marker_summary = (
        f"lib_marker format={payload.marker.format} lib_version={payload.marker.lib_version} "
        f"at 0x{payload.marker.offset:x}"
        if payload.marker is not None
        else "no library marker"
    )
    logger.info(
        "artifact %s (%d bytes, %s/%s, type %s, layout %s, %s) %s by %s",
        payload.digest,
        len(payload.data),
        target_q.target,
        target_q.version,
        target_q.artifact_type,
        payload.stored_layout,
        marker_summary,
        "stored" if created else "re-uploaded unchanged",
        token_id,
    )


@router.post(
    "",
    response_model=ArtifactUploaded,
    status_code=status.HTTP_201_CREATED,
    summary="Upload a firmware artifact",
)
async def upload_artifact(
    request: Request,
    response: Response,
    admin: AdminDep,
    services: Annotated[_UploadServices, Depends(_upload_services)],
    query: Annotated[_UploadQuery, Depends(_parse_upload_query)],
) -> ArtifactUploaded:
    """Store an opaque firmware blob and label it `(target, version)`."""
    repo_url, build_url = _validate_version_and_links(query)
    tq = query.target_q
    async with services.sessionmaker() as session:
        target_project = await resolve_project_for_request(
            session,
            admin,
            project_slug=tq.project,
            min_role=ProjectRole.OPERATOR,
            required_scope="write:artifacts",
        )
        catalog = await load_layout_catalog(session)

    limit, stored_layout = _resolve_slot_limit(request, tq, catalog)
    data = await _read_validated_body(request, limit, tq, admin.token_id)
    digest = digest_bytes(data)
    marker = find_marker(data) if tq.artifact_type == "app" else None
    has_lib_marker: bool | None = (marker is not None) if tq.artifact_type == "app" else None
    verified_sig = _verify_or_sign_digest(digest, query.options.sig, services.settings)
    payload = _PreparedPayload(
        data=data,
        digest=digest,
        stored_layout=stored_layout,
        marker=marker,
        has_lib_marker=has_lib_marker,
        verified_sig=verified_sig,
        repo_url=repo_url,
        build_url=build_url,
    )

    async with services.sessionmaker() as session:
        stored_blob, created = await _upsert_blob_and_label(
            session, target_project.id, query, payload, services
        )
        auto_deployed = await _trigger_post_upload_deploys(
            session, target_project, admin, query, services
        )

    if not created:
        response.status_code = status.HTTP_200_OK
    _log_upload_outcome(payload, tq, created, admin.token_id)
    return ArtifactUploaded(
        sha256=stored_blob.sha256,
        size_bytes=stored_blob.size_bytes,
        target=stored_blob.target,
        version=tq.version,
        partition_layout=stored_blob.partition_layout,
        created=created,
        has_lib_marker=stored_blob.has_lib_marker,
        artifact_type=stored_blob.artifact_type,
        sig=stored_blob.sig,
        repo_url=repo_url,
        branch=query.provenance.branch,
        commit_sha=query.provenance.commit_sha,
        tag=query.provenance.tag,
        build_url=build_url,
        test_passed=query.options.test_passed,
        auto_deployed_devices=auto_deployed,
        sim_status=getattr(stored_blob, "sim_status", None),
        sim_detail=getattr(stored_blob, "sim_detail", None),
        sim_runner=getattr(stored_blob, "sim_runner", None),
        sim_ran_at=getattr(stored_blob, "sim_ran_at", None),
    )

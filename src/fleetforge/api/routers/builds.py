"""Server-side container build pipeline routes & SSE log streaming (`R10-1`, `R10-2`).

Mounted at `/v1/projects/{slug}/builds`:
- `POST /v1/projects/{slug}/builds` — enqueue (or execute inline when `?run_inline=true`)
  a pinned ESP-IDF container build for `esp32`, `esp32s3`, `esp32c3`, or `esp32c6`.
- `GET /v1/projects/{slug}/builds` — list recent build jobs for the project.
- `GET /v1/projects/{slug}/builds/{build_id}` — inspect a single build job + `log_text`.
- `POST /v1/projects/{slug}/builds/{build_id}/retry` — re-enqueue a `failed` or `cancelled` job.
- `POST /v1/projects/{slug}/builds/{build_id}/cancel` — cancel a `queued` or `running` job.
- `GET /v1/projects/{slug}/builds/{build_id}/logs/stream` — stream incremental build log chunks
  over Server-Sent Events (`text/event-stream`) until completion.
"""

from __future__ import annotations

import asyncio
import json
import time
import uuid
from collections.abc import AsyncIterator
from pathlib import Path as FsPath
from typing import Annotated

from fastapi import APIRouter, Depends, HTTPException, Path, Query, Request, status
from fastapi.responses import StreamingResponse
from sqlalchemy import select

from fleetforge.api.deps import (
    AdminDep,
    BuildRunnerDep,
    ObjectStoreDep,
    SessionMakerDep,
    SettingsDep,
    SourceFetcherDep,
    bearer_scheme,
    cookie_scheme,
    resolve_project_for_request,
)
from fleetforge.api.schemas import (
    PROJECT_SLUG_PATTERN,
    BuildJobCreate,
    BuildJobDetail,
    BuildJobList,
    BuildJobSummary,
)
from fleetforge.builds import (
    TERMINAL_BUILD_STATUSES,
    BuildStatus,
    execute_build_job,
    resolve_pinned_idf_image,
)
from fleetforge.clock import now_utc
from fleetforge.db.models import BuildJob, ProjectRole
from fleetforge.partition_profiles import load_layout_catalog
from fleetforge.rollout_types import VERSION_PATTERN
from fleetforge.url_safety import sanitize_git_repo_url, validate_git_ref

router = APIRouter(
    prefix="/v1/projects/{slug}/builds",
    tags=["builds"],
    dependencies=[Depends(bearer_scheme), Depends(cookie_scheme)],
)

LIST_LIMIT = 50


def _to_summary(job: BuildJob) -> BuildJobSummary:
    return BuildJobSummary(
        id=job.id,
        project_id=job.project_id,
        repo_url=job.repo_url,
        git_ref=job.git_ref,
        commit_sha=job.commit_sha,
        target=job.target,
        version=job.version,
        partition_layout=job.partition_layout,
        idf_image=job.idf_image,
        cache_key=job.cache_key,
        status=job.status,  # type: ignore[arg-type]
        cache_hit=job.cache_hit,
        artifact_sha256=job.artifact_sha256,
        error_detail=job.error_detail,
        triggered_by=job.triggered_by,
        created_at=job.created_at,
        started_at=job.started_at,
        finished_at=job.finished_at,
    )


def _to_detail(job: BuildJob) -> BuildJobDetail:
    summary = _to_summary(job)
    return BuildJobDetail(**summary.model_dump(), log_text=job.log_text)


@router.get("", response_model=BuildJobList, summary="List recent server-side builds")
async def list_builds(
    slug: Annotated[str, Path(pattern=PROJECT_SLUG_PATTERN)],
    admin: AdminDep,
    sessionmaker: SessionMakerDep,
) -> BuildJobList:
    async with sessionmaker() as session:
        project = await resolve_project_for_request(
            session,
            admin,
            project_slug=slug,
            min_role=ProjectRole.VIEWER,
            required_scope="read",
        )
        rows = (
            await session.scalars(
                select(BuildJob)
                .where(BuildJob.project_id == project.id)
                .order_by(BuildJob.created_at.desc(), BuildJob.id.desc())
                .limit(LIST_LIMIT)
            )
        ).all()
    return BuildJobList(builds=[_to_summary(row) for row in rows])


@router.post(
    "",
    response_model=BuildJobSummary,
    status_code=status.HTTP_201_CREATED,
    summary="Queue (or execute) a server-side ESP-IDF container build",
)
async def create_build(
    slug: Annotated[str, Path(pattern=PROJECT_SLUG_PATTERN)],
    body: BuildJobCreate,
    admin: AdminDep,
    sessionmaker: SessionMakerDep,
    settings: SettingsDep,
    store: ObjectStoreDep,
    source_fetcher: SourceFetcherDep,
    build_runner: BuildRunnerDep,
    run_inline: Annotated[
        bool,
        Query(description="Execute the build job immediately before returning"),
    ] = False,
) -> BuildJobSummary:
    if not VERSION_PATTERN.match(body.version):
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail=(
                "version must be 1-64 characters of letters, digits, '.', '_', '+' or "
                "'-', starting with a letter or digit"
            ),
        )
    try:
        idf_image = resolve_pinned_idf_image(body.target)
    except ValueError as exc:
        raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail=str(exc)) from None

    async with sessionmaker() as session:
        project = await resolve_project_for_request(
            session,
            admin,
            project_slug=slug,
            min_role=ProjectRole.OPERATOR,
            required_scope=("write:artifacts", "deploy"),
        )
        catalog = await load_layout_catalog(session)
        if body.partition_layout not in catalog.layouts:
            known = ", ".join(sorted(catalog.layouts))
            raise HTTPException(
                status_code=status.HTTP_400_BAD_REQUEST,
                detail=f"unknown partition_layout; this server understands: {known}",
            )
        effective_repo = (body.repo_url or project.repo_url or "").strip()
        if not effective_repo:
            raise HTTPException(
                status_code=status.HTTP_400_BAD_REQUEST,
                detail="repo_url is required when the project has no configured repo_url",
            )
        allow_local = bool(getattr(source_fetcher, "allow_local_paths", False))
        if allow_local and FsPath(effective_repo).exists():
            clean_repo = effective_repo
        else:
            try:
                clean_repo = sanitize_git_repo_url(effective_repo, field_name="repo_url")
            except ValueError as exc:
                raise HTTPException(
                    status_code=status.HTTP_400_BAD_REQUEST, detail=str(exc)
                ) from None

        job = BuildJob(
            project_id=project.id,
            repo_url=clean_repo,
            git_ref=body.git_ref,
            target=body.target,
            version=body.version,
            partition_layout=body.partition_layout,
            idf_image=idf_image,
            status=BuildStatus.QUEUED.value,
            triggered_by=admin.subject,
        )
        session.add(job)
        await session.commit()
        await session.refresh(job)
        job_id = job.id

    if run_inline:
        completed = await execute_build_job(
            job_id,
            sessionmaker=sessionmaker,
            store=store,
            settings=settings,
            source_fetcher=source_fetcher,
            build_runner=build_runner,
        )
        if completed is not None:
            return _to_summary(completed)

    return _to_summary(job)


@router.get("/{build_id}", response_model=BuildJobDetail, summary="Inspect build job and logs")
async def get_build(
    slug: Annotated[str, Path(pattern=PROJECT_SLUG_PATTERN)],
    build_id: uuid.UUID,
    admin: AdminDep,
    sessionmaker: SessionMakerDep,
) -> BuildJobDetail:
    async with sessionmaker() as session:
        project = await resolve_project_for_request(
            session,
            admin,
            project_slug=slug,
            min_role=ProjectRole.VIEWER,
            required_scope="read",
        )
        job = await session.get(BuildJob, build_id)
        if job is None or job.project_id != project.id:
            raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="build job not found")
        return _to_detail(job)


@router.post(
    "/{build_id}/cancel",
    response_model=BuildJobSummary,
    summary="Cancel a queued or running build job",
)
async def cancel_build(
    slug: Annotated[str, Path(pattern=PROJECT_SLUG_PATTERN)],
    build_id: uuid.UUID,
    admin: AdminDep,
    sessionmaker: SessionMakerDep,
) -> BuildJobSummary:
    async with sessionmaker() as session:
        project = await resolve_project_for_request(
            session,
            admin,
            project_slug=slug,
            min_role=ProjectRole.OPERATOR,
            required_scope=("write:artifacts", "deploy"),
        )
        job = await session.get(BuildJob, build_id)
        if job is None or job.project_id != project.id:
            raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="build job not found")
        if job.status in TERMINAL_BUILD_STATUSES:
            raise HTTPException(
                status_code=status.HTTP_409_CONFLICT,
                detail=f"build job is already {job.status}",
            )
        job.status = BuildStatus.CANCELLED.value
        job.lease_owner = None
        job.lease_expires_at = None
        job.finished_at = now_utc()
        await session.commit()
        await session.refresh(job)
        return _to_summary(job)


@router.post(
    "/{build_id}/retry",
    response_model=BuildJobSummary,
    status_code=status.HTTP_201_CREATED,
    summary="Retry a failed or cancelled build job",
)
async def retry_build(
    slug: Annotated[str, Path(pattern=PROJECT_SLUG_PATTERN)],
    build_id: uuid.UUID,
    admin: AdminDep,
    sessionmaker: SessionMakerDep,
    settings: SettingsDep,
    store: ObjectStoreDep,
    source_fetcher: SourceFetcherDep,
    build_runner: BuildRunnerDep,
    run_inline: Annotated[
        bool,
        Query(description="Execute the retried build job immediately"),
    ] = False,
) -> BuildJobSummary:
    async with sessionmaker() as session:
        project = await resolve_project_for_request(
            session,
            admin,
            project_slug=slug,
            min_role=ProjectRole.OPERATOR,
            required_scope=("write:artifacts", "deploy"),
        )
        prev = await session.get(BuildJob, build_id)
        if prev is None or prev.project_id != project.id:
            raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="build job not found")
        if prev.status not in (BuildStatus.FAILED.value, BuildStatus.CANCELLED.value):
            raise HTTPException(
                status_code=status.HTTP_409_CONFLICT,
                detail=f"only failed or cancelled builds can be retried (current status: {prev.status})",
            )
        allow_local = bool(getattr(source_fetcher, "allow_local_paths", False))
        try:
            clean_ref = validate_git_ref(prev.git_ref)
            clean_repo = (
                prev.repo_url
                if (allow_local and FsPath(prev.repo_url).exists())
                else sanitize_git_repo_url(prev.repo_url, field_name="repo_url")
            )
        except ValueError as exc:
            raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail=str(exc)) from None
        retried = BuildJob(
            project_id=project.id,
            repo_url=clean_repo,
            git_ref=clean_ref,
            target=prev.target,
            version=prev.version,
            partition_layout=prev.partition_layout,
            idf_image=prev.idf_image,
            status=BuildStatus.QUEUED.value,
            triggered_by=admin.subject,
        )
        session.add(retried)
        await session.commit()
        await session.refresh(retried)
        new_id = retried.id

    if run_inline:
        completed = await execute_build_job(
            new_id,
            sessionmaker=sessionmaker,
            store=store,
            settings=settings,
            source_fetcher=source_fetcher,
            build_runner=build_runner,
        )
        if completed is not None:
            return _to_summary(completed)

    return _to_summary(retried)


def _diff_rolling_log(prev: str, curr: str) -> str:
    """Compute newly appended bytes even after `right(log_text || chunk, cap)` rolls past 256 KiB."""
    if curr == prev:
        return ""
    if not prev:
        return curr
    if curr.startswith(prev):
        return curr[len(prev) :]
    anchor_len = min(len(prev), max(1, min(64, len(curr) // 4)))
    anchor = prev[-anchor_len:]
    pos = curr.rfind(anchor)
    while pos != -1:
        end = pos + anchor_len
        if prev.endswith(curr[:end]):
            return curr[end:]
        pos = curr.rfind(anchor, 0, pos)
    return curr


@router.get(
    "/{build_id}/logs/stream",
    response_class=StreamingResponse,
    summary="Stream live build logs over Server-Sent Events (SSE)",
    responses={
        200: {"content": {"text/event-stream": {}}},
        503: {"description": "Too many concurrent build log stream clients"},
    },
)
async def stream_build_logs(
    request: Request,
    slug: Annotated[str, Path(pattern=PROJECT_SLUG_PATTERN)],
    build_id: uuid.UUID,
    admin: AdminDep,
    settings: SettingsDep,
    sessionmaker: SessionMakerDep,
    poll_s: Annotated[float, Query(ge=0.1, le=5.0)] = 0.25,
) -> StreamingResponse:
    async with sessionmaker() as session:
        project = await resolve_project_for_request(
            session,
            admin,
            project_slug=slug,
            min_role=ProjectRole.VIEWER,
            required_scope="read",
        )
        initial = await session.get(BuildJob, build_id)
        if initial is None or initial.project_id != project.id:
            raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="build job not found")

    active_streams = int(getattr(request.app.state, "build_log_streams", 0))
    if active_streams >= settings.sse_max_clients:
        raise HTTPException(
            status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
            detail="too many build log stream clients",
            headers={"Retry-After": "5"},
        )
    request.app.state.build_log_streams = active_streams + 1

    async def body() -> AsyncIterator[bytes]:
        try:
            yield b": connected\nretry: 1000\n\n"
            last_log = ""
            offset = 0
            last_status: str | None = None
            deadline = time.monotonic() + float(settings.sse_max_stream_s)

            while time.monotonic() < deadline:
                if await request.is_disconnected():
                    break
                async with sessionmaker() as poll_session:
                    current = await poll_session.get(BuildJob, build_id)
                if current is None:
                    break

                log_now = current.log_text or ""
                chunk = _diff_rolling_log(last_log, log_now)
                done = current.status in TERMINAL_BUILD_STATUSES
                if chunk or current.status != last_status or done:
                    last_log = log_now
                    offset += len(chunk)
                    last_status = current.status
                    frame = json.dumps(
                        {
                            "build_id": str(build_id),
                            "status": current.status,
                            "offset": offset,
                            "chunk": chunk,
                            "done": done,
                            "artifact_sha256": current.artifact_sha256,
                            "error_detail": current.error_detail,
                        }
                    )
                    yield f"data: {frame}\n\n".encode()
                    if done:
                        break
                await asyncio.sleep(poll_s)
        finally:
            current_active = int(getattr(request.app.state, "build_log_streams", 1))
            request.app.state.build_log_streams = max(0, current_active - 1)

    return StreamingResponse(
        body(),
        media_type="text/event-stream",
        headers={"Cache-Control": "no-store", "X-Accel-Buffering": "no"},
    )

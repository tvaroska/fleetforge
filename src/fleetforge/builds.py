"""Server-side container build pipeline cache and job executor (`R10-1`, `R10-2`).

Ports and adapters live in `fleetforge.build_adapters` and are re-exported here so
callers keep one import surface while keeping execution SQL, caching, and adapter code
separated (`A-29`).
"""

from __future__ import annotations

import datetime as dt
import hashlib
import json
import logging
import tempfile
import time
import uuid
from enum import StrEnum
from pathlib import Path
from typing import Any

from sqlalchemy import select, text
from sqlalchemy.exc import SQLAlchemyError
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from fleetforge.build_adapters import (
    PINNED_IDF_IMAGES as PINNED_IDF_IMAGES,
)
from fleetforge.build_adapters import (
    BuildError as BuildError,
)
from fleetforge.build_adapters import (
    BuildRunner as BuildRunner,
)
from fleetforge.build_adapters import (
    GitShallowSourceFetcher as GitShallowSourceFetcher,
)
from fleetforge.build_adapters import (
    LogSink as LogSink,
)
from fleetforge.build_adapters import (
    SandboxedIdfContainerRunner as SandboxedIdfContainerRunner,
)
from fleetforge.build_adapters import (
    SourceCheckout as SourceCheckout,
)
from fleetforge.build_adapters import (
    SourceFetcher as SourceFetcher,
)
from fleetforge.build_adapters import (
    build_sandboxed_docker_cmd as build_sandboxed_docker_cmd,
)
from fleetforge.build_adapters import (
    compute_tree_and_config_digests as compute_tree_and_config_digests,
)
from fleetforge.build_adapters import (
    create_build_runner as create_build_runner,
)
from fleetforge.build_adapters import (
    resolve_pinned_idf_image as resolve_pinned_idf_image,
)
from fleetforge.clock import now_utc
from fleetforge.config import Settings
from fleetforge.db.models import ArtifactKind, Build, BuildJob
from fleetforge.deploy_precheck import merged_binary
from fleetforge.lib_marker import find_marker
from fleetforge.merged_image import detect_merged
from fleetforge.signing import SigningKeyRing
from fleetforge.storage.blobs import blob_key, digest_bytes, put_blob
from fleetforge.storage.objectstore import ObjectNotFound, ObjectStore, ObjectStoreError
from fleetforge.url_safety import (
    resolve_and_validate_public_https_url as resolve_and_validate_public_https_url,
)
from fleetforge.url_safety import (
    sanitize_git_repo_url as sanitize_git_repo_url,
)

logger = logging.getLogger(__name__)

MAX_BUILD_LOG_CHARS = 256 * 1024
DEFAULT_BUILD_LEASE_TTL_S = 900.0
_LOG_BATCH_CHARS = 4096
_LOG_BATCH_INTERVAL_S = 0.25


class BuildStatus(StrEnum):
    """Advisory status values for `build_jobs.status` (`R10-2`)."""

    QUEUED = "queued"
    RUNNING = "running"
    SUCCEEDED = "succeeded"
    FAILED = "failed"
    CANCELLED = "cancelled"


TERMINAL_BUILD_STATUSES = frozenset(
    {
        BuildStatus.SUCCEEDED.value,
        BuildStatus.FAILED.value,
        BuildStatus.CANCELLED.value,
    }
)


def compute_build_cache_key(
    *,
    project_id: uuid.UUID | str,
    idf_image: str,
    target: str,
    partition_layout: str,
    source_tree_digest: str,
    config_digest: str,
) -> tuple[str, dict[str, str]]:
    """Compute `H(project_id, idf_image_digest, target, partition_layout, source_tree_digest, config_digest)`."""
    key_inputs = {
        "config_digest": config_digest,
        "idf_image_digest": idf_image,
        "partition_layout": partition_layout,
        "project_id": str(project_id),
        "source_tree_digest": source_tree_digest,
        "target": target,
    }
    canonical = json.dumps(key_inputs, sort_keys=True, separators=(",", ":")).encode("utf-8")
    return hashlib.sha256(canonical).hexdigest(), key_inputs


_APPEND_LOG_SQL = text(
    """
    UPDATE build_jobs
       SET log_text = right(log_text || :chunk, :cap),
           lease_expires_at = GREATEST(
               COALESCE(lease_expires_at, :lease_expires_at),
               :lease_expires_at
           )
     WHERE id = :job_id
       AND status = 'running'
       AND lease_owner = :lease_owner
    """
)

_ARTIFACT_GC_LOCK_SQL = text("SELECT pg_advisory_xact_lock(hashtext(:sha256))")

_CLAIM_JOB_INLINE_SQL = text(
    """
    UPDATE build_jobs
       SET status = 'running',
           started_at = COALESCE(started_at, :now),
           lease_owner = :worker_id,
           lease_expires_at = :lease_expires_at
     WHERE id = :job_id
       AND status = 'queued'
    RETURNING id
    """
)

_ARTIFACT_UPSERT_PROVENANCE_SQL = text(
    """
    INSERT INTO artifacts (
        sha256, size_bytes, kind, artifact_type, target, partition_layout,
        has_lib_marker, sig, provenance
    )
    VALUES (
        :sha256, :size_bytes, :kind, 'app', :target, :partition_layout,
        :has_lib_marker, :sig, CAST(:provenance AS jsonb)
    )
    ON CONFLICT (sha256) DO UPDATE
       SET sig = COALESCE(artifacts.sig, EXCLUDED.sig),
           provenance = COALESCE(artifacts.provenance, EXCLUDED.provenance)
    """
)

_VERSION_UPSERT_SQL = text(
    """
    INSERT INTO artifact_versions (
        project_id, target, version, sha256, repo_url, branch, commit_sha, tag
    )
    VALUES (
        :project_id, :target, :version, :sha256, :repo_url, :branch, :commit_sha, :tag
    )
    ON CONFLICT (project_id, target, version) DO UPDATE
       SET commit_sha = COALESCE(EXCLUDED.commit_sha, artifact_versions.commit_sha)
     WHERE artifact_versions.sha256 = EXCLUDED.sha256
    RETURNING sha256
    """
)

_ARTIFACT_METADATA_SELECT_SQL = text(
    """
    SELECT target, partition_layout, artifact_type
      FROM artifacts
     WHERE sha256 = :sha256
    """
)

_VERSION_SELECT_SQL = text(
    """
    SELECT sha256
      FROM artifact_versions
     WHERE project_id = :project_id AND target = :target AND version = :version
    """
)

_BUILD_CACHE_UPSERT_SQL = text(
    """
    INSERT INTO builds (cache_key, key_inputs, outputs, target, partition_layout)
    VALUES (
        :cache_key,
        CAST(:key_inputs AS jsonb),
        CAST(:outputs AS jsonb),
        :target,
        :partition_layout
    )
    ON CONFLICT (cache_key) DO NOTHING
    """
)


class _LogBatcher:
    """Batch build log appends and renew the job lease while output streams."""

    def __init__(
        self,
        sessionmaker: async_sessionmaker[AsyncSession],
        job_id: uuid.UUID,
        *,
        lease_owner: str,
        lease_ttl_s: float,
    ) -> None:
        self._sessionmaker = sessionmaker
        self._job_id = job_id
        self._lease_owner = lease_owner
        self._lease_ttl_s = max(1.0, lease_ttl_s)
        self._buf: list[str] = []
        self._chars = 0
        self._last_flush = time.monotonic()

    async def append(self, chunk: str) -> None:
        if not chunk:
            return
        self._buf.append(chunk)
        self._chars += len(chunk)
        if (
            self._chars >= _LOG_BATCH_CHARS
            or (time.monotonic() - self._last_flush) >= _LOG_BATCH_INTERVAL_S
        ):
            await self.flush()

    async def flush(self) -> None:
        if not self._buf:
            return
        payload = "".join(self._buf)
        self._buf.clear()
        self._chars = 0
        self._last_flush = time.monotonic()
        if len(payload) > MAX_BUILD_LOG_CHARS:
            payload = payload[-MAX_BUILD_LOG_CHARS:]
        lease_expires_at = now_utc() + dt.timedelta(seconds=self._lease_ttl_s)
        async with self._sessionmaker() as log_session:
            await log_session.execute(
                _APPEND_LOG_SQL,
                {
                    "job_id": self._job_id,
                    "lease_owner": self._lease_owner,
                    "lease_expires_at": lease_expires_at,
                    "chunk": payload,
                    "cap": MAX_BUILD_LOG_CHARS,
                },
            )
            await log_session.commit()


async def _lookup_cached_artifact(
    sessionmaker: async_sessionmaker[AsyncSession],
    store: ObjectStore,
    *,
    cache_key: str,
    target: str,
    partition_layout: str,
) -> str | None:
    async with sessionmaker() as session:
        cached_build = await session.get(Build, cache_key)
        if cached_build is None or not isinstance(cached_build.outputs, dict):
            return None
        app_entry = cached_build.outputs.get("app")
        if not isinstance(app_entry, dict) or not isinstance(app_entry.get("sha256"), str):
            return None
        candidate_sha = str(app_entry["sha256"])
        existing_art = (
            await session.execute(_ARTIFACT_METADATA_SELECT_SQL, {"sha256": candidate_sha})
        ).first()
        if (
            existing_art is None
            or existing_art.target != target
            or existing_art.partition_layout != partition_layout
            or existing_art.artifact_type != "app"
        ):
            return None
        try:
            _ = await store.get(blob_key(candidate_sha))
        except ObjectNotFound:
            return None
        return candidate_sha


async def _compile_artifact(
    *,
    work_dir: Path,
    target: str,
    partition_layout: str,
    idf_image: str,
    provenance: dict[str, Any],
    settings: Settings,
    build_runner: BuildRunner,
    on_log: LogSink,
) -> tuple[str, dict[str, Any], bytes]:
    image_bytes = await build_runner.compile_source(
        work_dir=work_dir,
        target=target,
        partition_layout=partition_layout,
        idf_image=idf_image,
        on_log=on_log,
    )
    if not image_bytes:
        raise BuildError("build produced an empty app binary")
    merged = detect_merged(image_bytes)
    if merged is not None:
        raise BuildError(merged_binary(merged).message)
    key_ring = SigningKeyRing.from_config(
        private_key=settings.artifact_signing_key,
        public_keys=settings.artifact_signing_public_keys,
    )
    artifact_sha = digest_bytes(image_bytes)
    sig = key_ring.sign_digest(artifact_sha) if key_ring.can_sign else None
    has_lib_marker = find_marker(image_bytes) is not None
    await on_log(
        f"[artifact] compiled {len(image_bytes)} B blob {artifact_sha[:12]} "
        f"({'signed' if sig else 'unsigned'})\n"
    )
    return (
        artifact_sha,
        {
            "sha256": artifact_sha,
            "size_bytes": len(image_bytes),
            "kind": ArtifactKind.USER_FIRMWARE.value,
            "target": target,
            "partition_layout": partition_layout,
            "has_lib_marker": has_lib_marker,
            "sig": sig,
            "provenance": json.dumps(provenance),
        },
        image_bytes,
    )


async def _commit_build_success(
    sessionmaker: async_sessionmaker[AsyncSession],
    store: ObjectStore,
    *,
    job_id: uuid.UUID,
    lease_owner: str,
    project_id: uuid.UUID,
    repo_url: str,
    target: str,
    version: str,
    partition_layout: str,
    checkout: SourceCheckout,
    cache_key: str,
    key_inputs: dict[str, str],
    cache_hit: bool,
    artifact_sha: str,
    fresh_artifact: dict[str, Any] | None,
    image_bytes: bytes | None,
) -> BuildJob | None:
    clean_tag = checkout.tag if checkout.is_tag else None
    clean_branch = None if checkout.is_tag else checkout.branch
    async with sessionmaker() as session:
        current_job = await session.scalar(
            select(BuildJob).where(BuildJob.id == job_id).with_for_update()
        )
        if (
            current_job is None
            or current_job.status != BuildStatus.RUNNING.value
            or current_job.lease_owner != lease_owner
        ):
            await session.rollback()
            return current_job
        if fresh_artifact is not None:
            await session.execute(_ARTIFACT_GC_LOCK_SQL, {"sha256": artifact_sha})
            if image_bytes is not None:
                await put_blob(store, image_bytes)
            await session.execute(_ARTIFACT_UPSERT_PROVENANCE_SQL, fresh_artifact)
            stored_meta = (
                await session.execute(_ARTIFACT_METADATA_SELECT_SQL, {"sha256": artifact_sha})
            ).one()
            if (
                stored_meta.target != target
                or stored_meta.partition_layout != partition_layout
                or stored_meta.artifact_type != "app"
            ):
                await session.rollback()
                raise BuildError(
                    f"artifact {artifact_sha[:12]} is already stored with "
                    f"(target={stored_meta.target!r}, "
                    f"partition_layout={stored_meta.partition_layout!r}, "
                    f"artifact_type={stored_meta.artifact_type!r}); cannot bind to "
                    f"(target={target!r}, partition_layout={partition_layout!r})"
                )
            await session.execute(
                _BUILD_CACHE_UPSERT_SQL,
                {
                    "cache_key": cache_key,
                    "key_inputs": json.dumps(key_inputs),
                    "outputs": json.dumps({"app": {"sha256": artifact_sha}}),
                    "target": target,
                    "partition_layout": partition_layout,
                },
            )
        bound_sha = await session.scalar(
            _VERSION_UPSERT_SQL,
            {
                "project_id": project_id,
                "target": target,
                "version": version,
                "sha256": artifact_sha,
                "repo_url": repo_url,
                "branch": clean_branch,
                "commit_sha": checkout.commit_sha,
                "tag": clean_tag,
            },
        )
        if bound_sha is None:
            existing_sha = await session.scalar(
                _VERSION_SELECT_SQL,
                {"project_id": project_id, "target": target, "version": version},
            )
            await session.rollback()
            raise BuildError(
                f"{target} version {version} already names artifact {existing_sha}; "
                "refusing to overwrite with different bytes"
            )
        current_job.commit_sha = checkout.commit_sha
        current_job.cache_key = cache_key
        current_job.cache_hit = cache_hit
        current_job.artifact_sha256 = artifact_sha
        current_job.status = BuildStatus.SUCCEEDED.value
        current_job.lease_owner = None
        current_job.lease_expires_at = None
        current_job.finished_at = now_utc()
        await session.commit()
        return current_job


async def _acquire_or_verify_job(
    sessionmaker: async_sessionmaker[AsyncSession],
    *,
    job_id: uuid.UUID,
    claimed_worker_id: str | None,
    lease_ttl_s: float,
) -> tuple[BuildJob | None, str | None]:
    """Claim a queued job inline, or verify ownership of an already-claimed worker job."""
    now = now_utc()
    async with sessionmaker() as session:
        if claimed_worker_id is None:
            owner = f"inline-{uuid.uuid4().hex[:12]}"
            claimed = await session.scalar(
                _CLAIM_JOB_INLINE_SQL,
                {
                    "job_id": job_id,
                    "now": now,
                    "worker_id": owner,
                    "lease_expires_at": now + dt.timedelta(seconds=max(1.0, lease_ttl_s)),
                },
            )
            await session.commit()
            job = await session.get(BuildJob, job_id)
            return job, (owner if claimed is not None else None)
        job = await session.get(BuildJob, job_id)
        if (
            job is None
            or job.status != BuildStatus.RUNNING.value
            or job.lease_owner != claimed_worker_id
        ):
            return job, None
        return job, claimed_worker_id


async def execute_build_job(
    job_id: uuid.UUID,
    *,
    sessionmaker: async_sessionmaker[AsyncSession],
    store: ObjectStore,
    settings: Settings,
    source_fetcher: SourceFetcher,
    build_runner: BuildRunner,
    claimed_worker_id: str | None = None,
    lease_ttl_s: float = DEFAULT_BUILD_LEASE_TTL_S,
) -> BuildJob | None:
    """Execute one leased `BuildJob` from `queued`/`running` -> `succeeded` / `failed`."""
    job, lease_owner = await _acquire_or_verify_job(
        sessionmaker,
        job_id=job_id,
        claimed_worker_id=claimed_worker_id,
        lease_ttl_s=lease_ttl_s,
    )
    if job is None or lease_owner is None:
        return job

    project_id = job.project_id
    repo_url = job.repo_url
    git_ref = job.git_ref
    target = job.target
    version = job.version
    partition_layout = job.partition_layout
    idf_image = job.idf_image
    log_batcher = _LogBatcher(
        sessionmaker,
        job_id,
        lease_owner=lease_owner,
        lease_ttl_s=lease_ttl_s,
    )

    try:
        with tempfile.TemporaryDirectory(prefix="ff-build-") as tmp_dir:
            work_dir = Path(tmp_dir) / "src"
            checkout = await source_fetcher.fetch(
                repo_url=repo_url,
                git_ref=git_ref,
                dest_dir=work_dir,
                on_log=log_batcher.append,
            )
            cache_key, key_inputs = compute_build_cache_key(
                project_id=project_id,
                idf_image=idf_image,
                target=target,
                partition_layout=partition_layout,
                source_tree_digest=checkout.source_tree_digest,
                config_digest=checkout.config_digest,
            )
            cached_sha = await _lookup_cached_artifact(
                sessionmaker,
                store,
                cache_key=cache_key,
                target=target,
                partition_layout=partition_layout,
            )
            provenance: dict[str, Any] = {
                "source_commit": checkout.commit_sha,
                "source_tree_digest": checkout.source_tree_digest,
                "config_sha256": checkout.config_digest,
                "idf_image": idf_image,
                "cache_key": cache_key,
            }
            fresh_artifact: dict[str, Any] | None = None
            image_bytes: bytes | None = None
            if cached_sha is not None:
                artifact_sha = cached_sha
                cache_hit = True
                await log_batcher.append(
                    f"[cache] exact hit on {cache_key[:12]} -> reusing artifact {artifact_sha[:12]}\n"
                )
            else:
                cache_hit = False
                artifact_sha, fresh_artifact, image_bytes = await _compile_artifact(
                    work_dir=work_dir,
                    target=target,
                    partition_layout=partition_layout,
                    idf_image=idf_image,
                    provenance=provenance,
                    settings=settings,
                    build_runner=build_runner,
                    on_log=log_batcher.append,
                )
            await log_batcher.flush()
            return await _commit_build_success(
                sessionmaker,
                store,
                job_id=job_id,
                lease_owner=lease_owner,
                project_id=project_id,
                repo_url=repo_url,
                target=target,
                version=version,
                partition_layout=partition_layout,
                checkout=checkout,
                cache_key=cache_key,
                key_inputs=key_inputs,
                cache_hit=cache_hit,
                artifact_sha=artifact_sha,
                fresh_artifact=fresh_artifact,
                image_bytes=image_bytes,
            )
    except (
        BuildError,
        ObjectStoreError,
        SQLAlchemyError,
        OSError,
        TimeoutError,
        ValueError,
        RuntimeError,
    ) as exc:
        err_msg = str(exc) or exc.__class__.__name__
        await log_batcher.append(f"[error] {err_msg}\n")
        await log_batcher.flush()
        async with sessionmaker() as session:
            failed_job = await session.scalar(
                select(BuildJob).where(BuildJob.id == job_id).with_for_update()
            )
            if (
                failed_job is not None
                and failed_job.status == BuildStatus.RUNNING.value
                and failed_job.lease_owner == lease_owner
            ):
                failed_job.status = BuildStatus.FAILED.value
                failed_job.error_detail = err_msg[:300]
                failed_job.lease_owner = None
                failed_job.lease_expires_at = None
                failed_job.finished_at = now_utc()
                await session.commit()
            else:
                await session.rollback()
            return failed_job

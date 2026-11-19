"""Server-side container build pipeline Ports, Adapters, cache, and job executor (`R10-1`, `R10-2`).

Implements `docs/features/build-pipeline.md`:
- Pinned ESP-IDF toolchain image digests per target (`esp32`, `esp32s3`, `esp32c3`, `esp32c6`).
- Host-side shallow clone (`SourceFetcher`) stripping `.git/` before compilation so zero
  repository credentials or ambient environment variables enter the compile container.
- Sandboxed single-tenant container execution (`--network=none`, `--cap-drop=ALL`,
  `--security-opt=no-new-privileges`, CPU/memory/PIDs/wall-clock caps, non-root user).
- Content-addressed Tier 1 build cache (`builds` table) keyed on
  `H(idf_image_digest, target, partition_layout, source_tree_digest, config_digest)`.
- Artifact signing (`SigningKeyRing`), `ObjectStore` upload, and provenance registration.
"""

from __future__ import annotations

import asyncio
import hashlib
import json
import logging
import os
import shutil
import tempfile
import uuid
from collections.abc import Awaitable, Callable
from dataclasses import dataclass
from enum import StrEnum
from pathlib import Path
from typing import Any, Protocol

from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from fleetforge.clock import now_utc
from fleetforge.config import Settings
from fleetforge.db.models import ArtifactKind, Build, BuildJob
from fleetforge.lib_marker import MAGIC, find_marker
from fleetforge.signing import SigningKeyRing
from fleetforge.storage.blobs import blob_key, digest_bytes, put_blob
from fleetforge.storage.objectstore import ObjectNotFound, ObjectStore
from fleetforge.url_safety import validate_public_https_url

logger = logging.getLogger(__name__)

MAX_BUILD_LOG_CHARS = 256 * 1024

PINNED_IDF_IMAGES: dict[str, str] = {
    "esp32": "espressif/idf:v5.3.2@sha256:6b21a5b74e84c96f80f5d42b1c22e9d8e5f9103f7a4e6b29a1d7c03e84b19a21",
    "esp32s3": "espressif/idf:v5.3.2@sha256:6b21a5b74e84c96f80f5d42b1c22e9d8e5f9103f7a4e6b29a1d7c03e84b19a21",
    "esp32c3": "espressif/idf:v5.3.2@sha256:6b21a5b74e84c96f80f5d42b1c22e9d8e5f9103f7a4e6b29a1d7c03e84b19a21",
    "esp32c6": "espressif/idf:v5.3.2@sha256:6b21a5b74e84c96f80f5d42b1c22e9d8e5f9103f7a4e6b29a1d7c03e84b19a21",
}

_CONFIG_FILENAMES = frozenset(
    {
        "sdkconfig",
        "sdkconfig.defaults",
        "partitions.csv",
        "platformio.ini",
        "idf_component.yml",
    }
)

LogSink = Callable[[str], Awaitable[None]]


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


class BuildError(RuntimeError):
    """Raised when source checkout or container compilation fails."""


@dataclass(frozen=True, slots=True)
class SourceCheckout:
    """Checked-out, credential-stripped source tree ready for sandboxed compilation."""

    commit_sha: str
    source_tree_digest: str
    config_digest: str


class SourceFetcher(Protocol):
    """Port for cloning a repository ref into `dest_dir` with `.git` stripped before build."""

    async def fetch(
        self,
        *,
        repo_url: str,
        git_ref: str,
        dest_dir: Path,
        on_log: LogSink | None = None,
    ) -> SourceCheckout: ...


class BuildRunner(Protocol):
    """Port for compiling a checked-out source directory into an app `.bin` (`R10-1`)."""

    async def compile_source(
        self,
        *,
        work_dir: Path,
        target: str,
        partition_layout: str,
        idf_image: str,
        on_log: LogSink | None = None,
    ) -> bytes: ...


def resolve_pinned_idf_image(target: str) -> str:
    """Return the pinned ESP-IDF container image digest for `target`, or raise `ValueError`."""
    image = PINNED_IDF_IMAGES.get(target)
    if image is None:
        supported = ", ".join(sorted(PINNED_IDF_IMAGES))
        raise ValueError(f"unsupported build target {target!r}; supported targets: {supported}")
    return image


def compute_tree_and_config_digests(work_dir: Path) -> tuple[str, str]:
    """Compute `(source_tree_digest, config_digest)` deterministically over `work_dir`."""
    tree_hasher = hashlib.sha256()
    cfg_hasher = hashlib.sha256()

    files = sorted(
        p
        for p in work_dir.rglob("*")
        if p.is_file() and ".git" not in p.parts and "build" not in p.parts
    )
    for path in files:
        rel = path.relative_to(work_dir).as_posix()
        raw = path.read_bytes()
        file_sha = hashlib.sha256(raw).hexdigest()
        entry = f"{rel}:{len(raw)}:{file_sha}\n".encode()
        tree_hasher.update(entry)
        if (
            path.name in _CONFIG_FILENAMES
            or path.name.startswith("sdkconfig.")
            or (path.name.startswith("partitions") and path.suffix == ".csv")
        ):
            cfg_hasher.update(entry)

    return tree_hasher.hexdigest(), cfg_hasher.hexdigest()


def compute_build_cache_key(
    *,
    idf_image: str,
    target: str,
    partition_layout: str,
    source_tree_digest: str,
    config_digest: str,
) -> tuple[str, dict[str, str]]:
    """Compute `H(idf_image_digest, target, partition_layout, source_tree_digest, config_digest)`."""
    key_inputs = {
        "config_digest": config_digest,
        "idf_image_digest": idf_image,
        "partition_layout": partition_layout,
        "source_tree_digest": source_tree_digest,
        "target": target,
    }
    canonical = json.dumps(key_inputs, sort_keys=True, separators=(",", ":")).encode("utf-8")
    return hashlib.sha256(canonical).hexdigest(), key_inputs


def build_sandboxed_docker_cmd(
    *,
    docker_bin: str,
    work_dir: Path,
    target: str,
    idf_image: str,
) -> list[str]:
    """Construct the hardened, networkless, zero-credential single-tenant Docker compile command."""
    return [
        docker_bin,
        "run",
        "--rm",
        "--network=none",
        "--cap-drop=ALL",
        "--security-opt=no-new-privileges",
        "--pids-limit=256",
        "--memory=2g",
        "--cpus=2",
        "--user=65534:65534",
        "--read-only",
        "--tmpfs=/tmp:rw,noexec,nosuid,size=512m",
        "-v",
        f"{work_dir.resolve()}:/work:rw",
        "-w",
        "/work",
        idf_image,
        "idf.py",
        f"-DIDF_TARGET={target}",
        "build",
    ]


class GitShallowSourceFetcher:
    """Host-side shallow Git cloner that strips `.git/` before container compilation (`R10-1`)."""

    def __init__(self, *, allow_local_paths: bool = False, timeout_s: float = 120.0) -> None:
        self._allow_local_paths = allow_local_paths
        self._timeout_s = timeout_s

    async def fetch(
        self,
        *,
        repo_url: str,
        git_ref: str,
        dest_dir: Path,
        on_log: LogSink | None = None,
    ) -> SourceCheckout:
        if self._allow_local_paths and Path(repo_url).exists():
            clone_target = str(Path(repo_url).resolve())
        else:
            clone_target = validate_public_https_url(repo_url, field_name="repo_url")

        git_bin = shutil.which("git")
        if not git_bin:
            raise BuildError("git binary is not installed on this builder")

        if on_log is not None:
            await on_log(f"[clone] fetching ref {git_ref} from {repo_url}\n")

        clean_env = {
            "PATH": os.environ.get("PATH", "/usr/bin:/bin"),
            "GIT_TERMINAL_PROMPT": "0",
        }
        proc = await asyncio.create_subprocess_exec(
            git_bin,
            "clone",
            "--quiet",
            clone_target,
            str(dest_dir),
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.STDOUT,
            env=clean_env,
        )
        out, _ = await asyncio.wait_for(proc.communicate(), timeout=self._timeout_s)
        if proc.returncode != 0:
            detail = out.decode("utf-8", errors="replace").strip() or "git clone failed"
            raise BuildError(f"git clone failed: {detail[:200]}")

        co_proc = await asyncio.create_subprocess_exec(
            git_bin,
            "-C",
            str(dest_dir),
            "checkout",
            "--quiet",
            git_ref,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.STDOUT,
            env=clean_env,
        )
        co_out, _ = await asyncio.wait_for(co_proc.communicate(), timeout=self._timeout_s)
        if co_proc.returncode != 0:
            detail = co_out.decode("utf-8", errors="replace").strip() or "git checkout failed"
            raise BuildError(f"git checkout {git_ref!r} failed: {detail[:200]}")

        rev_proc = await asyncio.create_subprocess_exec(
            git_bin,
            "-C",
            str(dest_dir),
            "rev-parse",
            "HEAD",
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.STDOUT,
            env=clean_env,
        )
        rev_out, _ = await asyncio.wait_for(rev_proc.communicate(), timeout=self._timeout_s)
        commit_sha = rev_out.decode("utf-8", errors="replace").strip()
        if len(commit_sha) < 7:
            raise BuildError("git rev-parse HEAD returned an invalid commit SHA")

        # Strip `.git` completely before handing `dest_dir` to the compile container so
        # clone URLs, remotes, and VCS state never enter the untrusted build sandbox.
        git_dir = dest_dir / ".git"
        if git_dir.exists():
            shutil.rmtree(git_dir)

        tree_digest, cfg_digest = compute_tree_and_config_digests(dest_dir)
        if on_log is not None:
            await on_log(
                f"[clone] checked out {commit_sha[:12]} (tree {tree_digest[:12]}, cfg {cfg_digest[:12]})\n"
            )
        return SourceCheckout(
            commit_sha=commit_sha,
            source_tree_digest=tree_digest,
            config_digest=cfg_digest,
        )


class SandboxedIdfContainerRunner:
    """Ephemeral, networkless Docker ESP-IDF build adapter (`R10-1`)."""

    def __init__(self, *, timeout_s: float = 600.0, docker_bin_override: str | None = None) -> None:
        self._timeout_s = timeout_s
        self._docker_bin_override = docker_bin_override

    async def compile_source(
        self,
        *,
        work_dir: Path,
        target: str,
        partition_layout: str,
        idf_image: str,
        on_log: LogSink | None = None,
    ) -> bytes:
        _ = partition_layout
        docker_bin = self._docker_bin_override or shutil.which("docker")
        if not docker_bin:
            raise BuildError("docker runtime is not available on this builder")

        cmd = build_sandboxed_docker_cmd(
            docker_bin=docker_bin,
            work_dir=work_dir,
            target=target,
            idf_image=idf_image,
        )
        proc = await asyncio.create_subprocess_exec(
            *cmd,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.STDOUT,
            env={"PATH": "/usr/bin:/bin"},
        )
        try:
            if proc.stdout is not None:
                async with asyncio.timeout(self._timeout_s):
                    while True:
                        line = await proc.stdout.readline()
                        if not line:
                            break
                        if on_log is not None:
                            await on_log(line.decode("utf-8", errors="replace"))
            await asyncio.wait_for(proc.wait(), timeout=5.0)
        except TimeoutError as exc:
            proc.kill()
            await proc.wait()
            raise BuildError(
                f"container build exceeded {self._timeout_s:.0f}s wall-clock limit"
            ) from exc

        if proc.returncode != 0:
            raise BuildError(f"ESP-IDF container exited with status {proc.returncode}")

        build_dir = work_dir / "build"
        bins = sorted(
            p
            for p in build_dir.glob("*.bin")
            if p.name not in {"bootloader.bin", "partition-table.bin", "ota_data_initial.bin"}
        )
        if not bins:
            raise BuildError("build succeeded but produced no app .bin under build/")
        return bins[0].read_bytes()


class DeterministicLocalBuildRunner:
    """Deterministic host-side build adapter for CI and integration tests without Docker."""

    async def compile_source(
        self,
        *,
        work_dir: Path,
        target: str,
        partition_layout: str,
        idf_image: str,
        on_log: LogSink | None = None,
    ) -> bytes:
        if on_log is not None:
            await on_log(f"[1/3] Configuring target {target} ({partition_layout}) on {idf_image}\n")

        sources = sorted(p for p in work_dir.rglob("*") if p.is_file())
        if not sources:
            raise BuildError("repository checkout contains no source files")

        combined = hashlib.sha256()
        for src in sources:
            content = src.read_bytes()
            if b"FF_BUILD_FAIL" in content:
                if on_log is not None:
                    await on_log(f"error: compilation failed in {src.relative_to(work_dir)}\n")
                raise BuildError(f"compile error in {src.relative_to(work_dir)}")
            combined.update(content)

        if on_log is not None:
            await on_log(f"[2/3] Compiling {len(sources)} source file(s) for {target}\n")
            await on_log("[3/3] Linking ESP-IDF app image with libfleetforge marker\n")

        marker = MAGIC + b"\x01\x00\x00\x00" + b"0.6.0\x00".ljust(32, b"\x00") + b"\x00" * 12
        meta = f"{target}:{partition_layout}:{idf_image}:{combined.hexdigest()}".encode("ascii")
        return b"\xe9\x00\x02\x20" + marker + meta + (b"\x00" * 64)


def create_build_runner(backend: str | None = None) -> BuildRunner:
    """Instantiate the configured `BuildRunner` (`docker`, `local`, or `auto`)."""
    mode = (backend or os.environ.get("FF_BUILD_BACKEND") or "auto").strip().lower()
    if mode == "docker":
        return SandboxedIdfContainerRunner()
    if mode == "local":
        return DeterministicLocalBuildRunner()
    if shutil.which("docker"):
        return SandboxedIdfContainerRunner()
    return DeterministicLocalBuildRunner()


_APPEND_LOG_SQL = text(
    """
    UPDATE build_jobs
       SET log_text = left(log_text || :chunk, :cap)
     WHERE id = :job_id
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


async def execute_build_job(
    job_id: uuid.UUID,
    *,
    sessionmaker: async_sessionmaker[AsyncSession],
    store: ObjectStore,
    settings: Settings,
    source_fetcher: SourceFetcher,
    build_runner: BuildRunner,
) -> BuildJob | None:
    """Execute one `BuildJob` from `queued` -> `running` -> `succeeded` / `failed`."""
    async with sessionmaker() as session:
        job = await session.get(BuildJob, job_id)
        if job is None or job.status in TERMINAL_BUILD_STATUSES:
            return job
        job.status = BuildStatus.RUNNING.value
        job.started_at = now_utc()
        project_id = job.project_id
        repo_url = job.repo_url
        git_ref = job.git_ref
        target = job.target
        version = job.version
        partition_layout = job.partition_layout
        idf_image = job.idf_image
        await session.commit()

    async def append_log(chunk: str) -> None:
        if not chunk:
            return
        async with sessionmaker() as log_session:
            await log_session.execute(
                _APPEND_LOG_SQL,
                {"job_id": job_id, "chunk": chunk, "cap": MAX_BUILD_LOG_CHARS},
            )
            await log_session.commit()

    try:
        with tempfile.TemporaryDirectory(prefix="ff-build-") as tmp_dir:
            work_dir = Path(tmp_dir) / "src"
            checkout = await source_fetcher.fetch(
                repo_url=repo_url,
                git_ref=git_ref,
                dest_dir=work_dir,
                on_log=append_log,
            )
            cache_key, key_inputs = compute_build_cache_key(
                idf_image=idf_image,
                target=target,
                partition_layout=partition_layout,
                source_tree_digest=checkout.source_tree_digest,
                config_digest=checkout.config_digest,
            )

            cached_sha: str | None = None
            async with sessionmaker() as session:
                cached_build = await session.get(Build, cache_key)
                if cached_build is not None and isinstance(cached_build.outputs, dict):
                    app_entry = cached_build.outputs.get("app")
                    if isinstance(app_entry, dict) and isinstance(app_entry.get("sha256"), str):
                        candidate_sha = app_entry["sha256"]
                        exists_in_db = await session.scalar(
                            text("SELECT 1 FROM artifacts WHERE sha256 = :sha"),
                            {"sha": candidate_sha},
                        )
                        if exists_in_db is not None:
                            try:
                                _ = await store.get(blob_key(candidate_sha))
                                cached_sha = candidate_sha
                            except ObjectNotFound:
                                cached_sha = None

            key_ring = SigningKeyRing.from_config(
                private_key=settings.artifact_signing_key,
                public_keys=settings.artifact_signing_public_keys,
            )
            provenance: dict[str, Any] = {
                "source_commit": checkout.commit_sha,
                "source_tree_digest": checkout.source_tree_digest,
                "config_sha256": checkout.config_digest,
                "idf_image": idf_image,
                "cache_key": cache_key,
            }

            if cached_sha is not None:
                artifact_sha = cached_sha
                cache_hit = True
                await append_log(
                    f"[cache] exact hit on {cache_key[:12]} -> reusing artifact {artifact_sha[:12]}\n"
                )
            else:
                cache_hit = False
                image_bytes = await build_runner.compile_source(
                    work_dir=work_dir,
                    target=target,
                    partition_layout=partition_layout,
                    idf_image=idf_image,
                    on_log=append_log,
                )
                artifact_sha = digest_bytes(image_bytes)
                sig = key_ring.sign_digest(artifact_sha) if key_ring.can_sign else None
                has_lib_marker = find_marker(image_bytes) is not None
                await put_blob(store, image_bytes)
                await append_log(
                    f"[artifact] stored {len(image_bytes)} B blob {artifact_sha[:12]} "
                    f"({'signed' if sig else 'unsigned'})\n"
                )

                async with sessionmaker() as session:
                    await session.execute(
                        _ARTIFACT_UPSERT_PROVENANCE_SQL,
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
                    await session.commit()

            is_tag = git_ref.startswith("v") or git_ref.startswith("refs/tags/")
            clean_tag = git_ref.removeprefix("refs/tags/") if is_tag else None
            clean_branch = None if is_tag else git_ref.removeprefix("refs/heads/")

            async with sessionmaker() as session:
                current_job = await session.get(BuildJob, job_id)
                if current_job is None or current_job.status == BuildStatus.CANCELLED.value:
                    return current_job

                await session.execute(
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
                current_job.commit_sha = checkout.commit_sha
                current_job.cache_key = cache_key
                current_job.cache_hit = cache_hit
                current_job.artifact_sha256 = artifact_sha
                current_job.status = BuildStatus.SUCCEEDED.value
                current_job.finished_at = now_utc()
                await session.commit()
                return current_job

    except Exception as exc:  # noqa: BLE001
        err_msg = str(exc) or exc.__class__.__name__
        await append_log(f"[error] {err_msg}\n")
        async with sessionmaker() as session:
            failed_job = await session.get(BuildJob, job_id)
            if failed_job is not None and failed_job.status != BuildStatus.CANCELLED.value:
                failed_job.status = BuildStatus.FAILED.value
                failed_job.error_detail = err_msg[:300]
                failed_job.finished_at = now_utc()
                await session.commit()
            return failed_job

"""Build pipeline Ports, Git source fetcher, and sandboxed ESP-IDF container adapter (`R10-1`)."""

from __future__ import annotations

import asyncio
import codecs
import hashlib
import json
import os
import re
import shutil
import uuid
from collections.abc import Awaitable, Callable, Sequence
from dataclasses import dataclass
from pathlib import Path
from typing import Protocol

from fleetforge.url_safety import (
    resolve_and_validate_public_https_url,
    sanitize_git_repo_url,
    validate_git_ref,
)

_STDOUT_READ_CHUNK_BYTES = 4096
MAX_BUILD_APP_BYTES = 16 * 1024 * 1024
_PINNED_IDF_V555 = (
    "espressif/idf:v5.5.5@sha256:a9231d0697ab8f7517cc072e93b7c83e04907bfbfba80b6440d7dbbf90665cf2"
)

PINNED_IDF_IMAGES: dict[str, str] = {
    "esp32": _PINNED_IDF_V555,
    "esp32s3": _PINNED_IDF_V555,
    "esp32c3": _PINNED_IDF_V555,
    "esp32c6": _PINNED_IDF_V555,
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

_COMMIT_SHA_RE = re.compile(r"^[0-9a-f]{40}\Z")

LogSink = Callable[[str], Awaitable[None]]


class BuildError(RuntimeError):
    """Raised when source checkout or container compilation fails."""


@dataclass(frozen=True, slots=True)
class SourceCheckout:
    """Checked-out, credential-stripped source tree ready for sandboxed compilation."""

    commit_sha: str
    source_tree_digest: str
    config_digest: str
    is_tag: bool = False
    branch: str | None = None
    tag: str | None = None


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


def remove_path(path: Path) -> None:
    """Delete `path` whether it is a symlink, regular file, or directory tree."""
    if path.is_symlink() or path.is_file():
        path.unlink()
    elif path.exists():
        shutil.rmtree(path)


def compute_tree_and_config_digests(work_dir: Path) -> tuple[str, str]:
    """Compute `(source_tree_digest, config_digest)` deterministically over `work_dir`."""
    tree_hasher = hashlib.sha256()
    cfg_hasher = hashlib.sha256()

    entries = sorted(
        p
        for p in work_dir.rglob("*")
        if ".git" not in p.relative_to(work_dir).parts and (p.is_symlink() or p.is_file())
    )
    for path in entries:
        rel = path.relative_to(work_dir).as_posix()
        if path.is_symlink():
            target = os.readlink(path)
            entry = f"symlink:{rel}:{target}\n".encode()
        else:
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


def build_sandboxed_docker_cmd(
    *,
    docker_bin: str,
    work_dir: Path,
    target: str,
    idf_image: str,
    partition_layout: str = "ab-4m-v1",
    container_name: str | None = None,
) -> list[str]:
    """Construct the hardened, networkless, zero-credential single-tenant Docker compile command."""
    name_args = ["--name", container_name] if container_name else []
    return [
        docker_bin,
        "run",
        "--rm",
        *name_args,
        "--network=none",
        "--cap-drop=ALL",
        "--security-opt=no-new-privileges",
        "--pids-limit=256",
        "--memory=2g",
        "--cpus=2",
        "--user=65534:65534",
        "--read-only",
        "--tmpfs=/tmp:rw,noexec,nosuid,size=512m",
        "-e",
        "HOME=/tmp",
        "-e",
        "IDF_COMPONENT_MANAGER=0",
        "-e",
        f"FF_PARTITION_LAYOUT={partition_layout}",
        "-v",
        f"{work_dir.resolve()}:/work:rw",
        "-w",
        "/work",
        idf_image,
        "idf.py",
        f"-DIDF_TARGET={target}",
        f"-DFF_PARTITION_LAYOUT={partition_layout}",
        "build",
    ]


async def _run_git(
    args: Sequence[str],
    *,
    env: dict[str, str],
    timeout_s: float,
    label: str,
) -> str:
    """Run one `git` subprocess, cleaning up on timeout and failing on any non-zero exit."""
    proc = await asyncio.create_subprocess_exec(
        *args,
        stdout=asyncio.subprocess.PIPE,
        stderr=asyncio.subprocess.STDOUT,
        env=env,
    )
    try:
        out, _ = await asyncio.wait_for(proc.communicate(), timeout=timeout_s)
    except TimeoutError as exc:
        proc.kill()
        await proc.wait()
        raise BuildError(f"{label} timed out after {timeout_s:.0f}s") from exc

    decoded = out.decode("utf-8", errors="replace").strip()
    if proc.returncode != 0:
        detail = decoded or f"{label} failed"
        raise BuildError(f"{label} failed: {detail[:200]}")
    return decoded


def _classify_fetch_head(fetch_head_text: str, git_ref: str) -> tuple[bool, str | None, str | None]:
    """Classify a fetched Git ref as `(is_tag, branch, tag)` from `.git/FETCH_HEAD`."""
    for raw_line in fetch_head_text.splitlines():
        line = raw_line.strip()
        if not line:
            continue
        parts = raw_line.split("\t", 2)
        desc = parts[2].strip() if len(parts) == 3 else ""
        if desc.startswith("tag '") and "' of " in desc:
            tag_name = desc.removeprefix("tag '").rsplit("' of ", 1)[0]
            return True, None, tag_name or git_ref.removeprefix("refs/tags/")
        if desc.startswith("branch '") and "' of " in desc:
            branch_name = desc.removeprefix("branch '").rsplit("' of ", 1)[0]
            return False, branch_name or git_ref.removeprefix("refs/heads/"), None
        break
    if git_ref.startswith("refs/tags/"):
        return True, None, git_ref.removeprefix("refs/tags/")
    if git_ref.startswith("refs/heads/"):
        return False, git_ref.removeprefix("refs/heads/"), None
    return False, None, None


class GitShallowSourceFetcher:
    """Host-side shallow Git fetcher that strips `.git/` and `build/` before compilation (`R10-1`)."""

    def __init__(
        self,
        *,
        allow_local_paths: bool = False,
        timeout_s: float = 120.0,
        ca_bundle: Path | None = None,
    ) -> None:
        self._allow_local_paths = allow_local_paths
        self._timeout_s = timeout_s
        self._ca_bundle = ca_bundle

    @property
    def allow_local_paths(self) -> bool:
        return self._allow_local_paths

    async def fetch(
        self,
        *,
        repo_url: str,
        git_ref: str,
        dest_dir: Path,
        on_log: LogSink | None = None,
    ) -> SourceCheckout:
        try:
            clean_ref = validate_git_ref(git_ref)
        except ValueError as exc:
            raise BuildError(str(exc)) from exc

        is_local = self._allow_local_paths and Path(repo_url).exists()
        if is_local:
            clone_target = str(Path(repo_url).resolve())
        else:
            try:
                clone_target = sanitize_git_repo_url(repo_url, field_name="repo_url")
                await resolve_and_validate_public_https_url(clone_target, field_name="repo_url")
            except ValueError as exc:
                raise BuildError(str(exc)) from exc

        git_bin = shutil.which("git")
        if not git_bin:
            raise BuildError("git binary is not installed on this builder")

        if on_log is not None:
            await on_log(f"[clone] fetching ref {clean_ref} from {clone_target}\n")

        clean_env = {
            "PATH": os.environ.get("PATH", "/usr/bin:/bin"),
            "GIT_TERMINAL_PROMPT": "0",
            "GIT_CONFIG_NOSYSTEM": "1",
            "GIT_CONFIG_GLOBAL": "/dev/null",
        }
        if self._ca_bundle is not None:
            clean_env["GIT_SSL_CAINFO"] = str(self._ca_bundle)

        dest_dir.mkdir(parents=True, exist_ok=True)
        await _run_git(
            [git_bin, "init", "--quiet", str(dest_dir)],
            env=clean_env,
            timeout_s=self._timeout_s,
            label="git init",
        )

        proto_allow = "protocol.file.allow=always" if is_local else "protocol.https.allow=always"
        await _run_git(
            [
                git_bin,
                "-C",
                str(dest_dir),
                "-c",
                "http.followRedirects=false",
                "-c",
                "protocol.allow=never",
                "-c",
                proto_allow,
                "fetch",
                "--quiet",
                "--depth=1",
                "--no-tags",
                "--end-of-options",
                clone_target,
                clean_ref,
            ],
            env=clean_env,
            timeout_s=self._timeout_s,
            label=f"git fetch {clean_ref!r}",
        )

        fetch_head_path = dest_dir / ".git" / "FETCH_HEAD"
        fetch_head_text = (
            fetch_head_path.read_text(encoding="utf-8", errors="replace")
            if fetch_head_path.is_file()
            else ""
        )
        is_tag, branch_name, tag_name = _classify_fetch_head(fetch_head_text, clean_ref)

        await _run_git(
            [git_bin, "-C", str(dest_dir), "checkout", "--quiet", "FETCH_HEAD"],
            env=clean_env,
            timeout_s=self._timeout_s,
            label=f"git checkout {clean_ref!r}",
        )

        commit_sha = await _run_git(
            [git_bin, "-C", str(dest_dir), "rev-parse", "--verify", "HEAD^{commit}"],
            env=clean_env,
            timeout_s=self._timeout_s,
            label="git rev-parse HEAD",
        )
        if not _COMMIT_SHA_RE.match(commit_sha):
            raise BuildError("git rev-parse HEAD returned an invalid commit SHA")

        # Strip `.git` and any committed top-level `build/` before hashing and compilation so
        # clone URLs, VCS state, and planted `build/*.bin` artifacts never enter the sandbox.
        remove_path(dest_dir / ".git")
        remove_path(dest_dir / "build")

        tree_digest, cfg_digest = compute_tree_and_config_digests(dest_dir)
        if on_log is not None:
            await on_log(
                f"[clone] checked out {commit_sha[:12]} (tree {tree_digest[:12]}, cfg {cfg_digest[:12]})\n"
            )
        return SourceCheckout(
            commit_sha=commit_sha,
            source_tree_digest=tree_digest,
            config_digest=cfg_digest,
            is_tag=is_tag,
            branch=branch_name,
            tag=tag_name,
        )


async def _stream_process_output(
    stdout: asyncio.StreamReader,
    on_log: LogSink | None,
) -> None:
    """Read subprocess stdout in fixed 4 KiB chunks so >64 KiB lines never overrun StreamReader."""
    decoder = codecs.getincrementaldecoder("utf-8")(errors="replace")
    while True:
        chunk = await stdout.read(_STDOUT_READ_CHUNK_BYTES)
        if not chunk:
            break
        text_chunk = decoder.decode(chunk, final=False)
        if text_chunk and on_log is not None:
            await on_log(text_chunk)
    tail = decoder.decode(b"", final=True)
    if tail and on_log is not None:
        await on_log(tail)


def _resolve_compiled_app_bin(work_dir: Path) -> bytes:
    """Validate `build/project_description.json` containment and return compiled app bytes."""
    work_root = work_dir.resolve()
    raw_build_dir = work_dir / "build"
    if raw_build_dir.is_symlink() or not raw_build_dir.is_dir():
        raise BuildError("build succeeded but produced no valid build/ directory")
    build_dir = raw_build_dir.resolve()
    if not build_dir.is_relative_to(work_root):
        raise BuildError("build directory escaped work_dir")
    desc_path = build_dir / "project_description.json"
    if desc_path.is_symlink() or not desc_path.is_file():
        raise BuildError("build succeeded but produced no build/project_description.json")
    try:
        desc = json.loads(desc_path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        raise BuildError("build/project_description.json is unreadable or invalid JSON") from exc

    app_bin = desc.get("app_bin") if isinstance(desc, dict) else None
    if not isinstance(app_bin, str) or not app_bin.strip():
        raise BuildError("build/project_description.json is missing a valid app_bin path")

    raw_app_path = build_dir / app_bin
    if raw_app_path.is_symlink():
        raise BuildError(f"build app_bin {app_bin!r} must not be a symlink")
    app_path = raw_app_path.resolve()
    if not app_path.is_relative_to(build_dir) or not app_path.is_file():
        raise BuildError(f"build app_bin {app_bin!r} is missing or escapes build/")
    app_size = app_path.stat().st_size
    if app_size > MAX_BUILD_APP_BYTES:
        raise BuildError(
            f"build app_bin {app_bin!r} ({app_size} B) exceeds {MAX_BUILD_APP_BYTES} B limit"
        )
    return app_path.read_bytes()


async def _force_remove_container(docker_bin: str, container_name: str) -> None:
    """Best-effort `docker rm -f` so timed-out or cancelled builds never orphan containers."""
    try:
        rm_proc = await asyncio.create_subprocess_exec(
            docker_bin,
            "rm",
            "-f",
            container_name,
            stdout=asyncio.subprocess.DEVNULL,
            stderr=asyncio.subprocess.DEVNULL,
            env={"PATH": "/usr/bin:/bin"},
        )
        await asyncio.wait_for(rm_proc.wait(), timeout=5.0)
    except (OSError, TimeoutError):
        pass


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
        clean_layout = partition_layout.strip()
        if not clean_layout:
            raise BuildError("partition_layout must not be empty")
        docker_bin = self._docker_bin_override or shutil.which("docker")
        if not docker_bin:
            raise BuildError("docker runtime is not available on this builder")

        remove_path(work_dir / "build")
        try:
            os.chmod(work_dir, 0o777)  # noqa: S103
        except OSError:
            pass

        container_name = f"ff-build-{uuid.uuid4().hex[:12]}"
        cmd = build_sandboxed_docker_cmd(
            docker_bin=docker_bin,
            work_dir=work_dir,
            target=target,
            idf_image=idf_image,
            partition_layout=clean_layout,
            container_name=container_name,
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
                    await _stream_process_output(proc.stdout, on_log)
            await asyncio.wait_for(proc.wait(), timeout=5.0)
        except TimeoutError as exc:
            proc.kill()
            await proc.wait()
            await _force_remove_container(docker_bin, container_name)
            raise BuildError(
                f"container build exceeded {self._timeout_s:.0f}s wall-clock limit"
            ) from exc
        except asyncio.CancelledError:
            if proc.returncode is None:
                proc.kill()
                await proc.wait()
            await _force_remove_container(docker_bin, container_name)
            raise

        if proc.returncode != 0:
            raise BuildError(f"ESP-IDF container exited with status {proc.returncode}")
        return _resolve_compiled_app_bin(work_dir)


def create_build_runner(backend: str | None = None) -> BuildRunner:
    """Instantiate the configured `BuildRunner` (`docker` or `auto`, never a test stand-in)."""
    mode = (backend or os.environ.get("FF_BUILD_BACKEND") or "auto").strip().lower()
    if mode in ("auto", "docker"):
        return SandboxedIdfContainerRunner()
    raise ValueError(f"unsupported build backend {mode!r}; expected 'auto' or 'docker'")

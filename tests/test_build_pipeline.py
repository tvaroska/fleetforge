"""Tests for `R10` Server-Side Container Build Pipeline (`R10-1`, `R10-2`).

Covers:
- Hardened sandboxed Docker invocation (`--network=none`, `--cap-drop=ALL`,
  `--security-opt=no-new-privileges`, CPU/memory/PIDs caps, non-root `65534:65534`,
  read-only rootfs, zero ambient credentials).
- `GitShallowSourceFetcher`: shallow local clone, ref checkout, `.git/` directory
  stripping prior to compilation, and deterministic `(source_tree_digest, config_digest)`.
- `SandboxedIdfContainerRunner` against a mock `docker` executable verifying isolated
  `env`, streamed stdout lines, and `build/*.bin` extraction.
- PostgreSQL end-to-end build queue -> `builder.run_once` -> signed `.bin` in `ObjectStore`,
  `artifacts.provenance` (`source_commit`, `idf_image`, `config_sha256`, `cache_key`),
  `artifact_versions` visible in `GET /v1/artifact`, Tier 1 `builds` cache hit without
  re-compiling, failed build (`FF_BUILD_FAIL`) -> `retry` & `cancel`, and SSE `/logs/stream`.
"""

from __future__ import annotations

import json
import os
import shutil
import stat
import subprocess
from pathlib import Path
from typing import Any

import pytest
from fastapi import FastAPI
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncEngine, async_sessionmaker

from fleetforge import builder
from fleetforge.api.deps import (
    get_build_runner,
    get_object_store,
    get_settings,
    get_source_fetcher,
)
from fleetforge.builds import (
    PINNED_IDF_IMAGES,
    BuildError,
    DeterministicLocalBuildRunner,
    GitShallowSourceFetcher,
    SandboxedIdfContainerRunner,
    build_sandboxed_docker_cmd,
    compute_build_cache_key,
    compute_tree_and_config_digests,
    resolve_pinned_idf_image,
)
from fleetforge.db.models import Artifact, Build, BuildJob
from fleetforge.signing import generate_keypair, verify_digest
from fleetforge.storage.blobs import blob_key
from tests.conftest import (
    MemoryObjectStore,
    client_for,
    login_admin,
    settings_for_tests,
)

_PRIV_A, _PUB_A = generate_keypair()
_PRIV_B, _PUB_B = generate_keypair()


def _init_git_repo(repo_dir: Path, files: dict[str, str], tag: str | None = None) -> str:
    repo_dir.mkdir(parents=True, exist_ok=True)
    git_bin = shutil.which("git") or "/usr/bin/git"
    env = {
        "PATH": os.environ.get("PATH", "/usr/bin:/bin"),
        "GIT_AUTHOR_NAME": "Test",
        "GIT_AUTHOR_EMAIL": "test@example.com",
        "GIT_COMMITTER_NAME": "Test",
        "GIT_COMMITTER_EMAIL": "test@example.com",
    }
    subprocess.run([git_bin, "init", "-q", "-b", "main", str(repo_dir)], check=True, env=env)  # noqa: S603
    for rel, content in files.items():
        target = repo_dir / rel
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_text(content, encoding="utf-8")
    subprocess.run([git_bin, "-C", str(repo_dir), "add", "."], check=True, env=env)  # noqa: S603
    subprocess.run(  # noqa: S603
        [git_bin, "-C", str(repo_dir), "commit", "-q", "-m", "initial"],
        check=True,
        env=env,
    )
    if tag:
        subprocess.run([git_bin, "-C", str(repo_dir), "tag", tag], check=True, env=env)  # noqa: S603
    out = subprocess.run(  # noqa: S603
        [git_bin, "-C", str(repo_dir), "rev-parse", "HEAD"],
        check=True,
        capture_output=True,
        text=True,
        env=env,
    )
    return out.stdout.strip()


class TestBuildSecurityAndCacheKey:
    def test_pinned_idf_images_cover_all_four_esp_targets(self) -> None:
        for target in ("esp32", "esp32s3", "esp32c3", "esp32c6"):
            img = resolve_pinned_idf_image(target)
            assert "@sha256:" in img
        with pytest.raises(ValueError, match="unsupported build target"):
            resolve_pinned_idf_image("stm32f4")

    def test_sandboxed_docker_cmd_enforces_networkless_nonroot_caps(self, tmp_path: Path) -> None:
        cmd = build_sandboxed_docker_cmd(
            docker_bin="/usr/bin/docker",
            work_dir=tmp_path,
            target="esp32s3",
            idf_image=PINNED_IDF_IMAGES["esp32s3"],
        )
        assert "--network=none" in cmd
        assert "--cap-drop=ALL" in cmd
        assert "--security-opt=no-new-privileges" in cmd
        assert "--pids-limit=256" in cmd
        assert "--memory=2g" in cmd
        assert "--cpus=2" in cmd
        assert "--user=65534:65534" in cmd
        assert "--read-only" in cmd
        assert "-DIDF_TARGET=esp32s3" in cmd

    def test_cache_key_changes_when_sdkconfig_or_source_tree_changes(self, tmp_path: Path) -> None:
        (tmp_path / "main").mkdir()
        (tmp_path / "main" / "main.c").write_text("int app_main(void) { return 0; }\n")
        (tmp_path / "sdkconfig.defaults").write_text("CONFIG_ESP_TASK_WDT_TIMEOUT_S=5\n")
        tree_a, cfg_a = compute_tree_and_config_digests(tmp_path)
        key_a, _ = compute_build_cache_key(
            idf_image=PINNED_IDF_IMAGES["esp32s3"],
            target="esp32s3",
            partition_layout="ab-4m-v1",
            source_tree_digest=tree_a,
            config_digest=cfg_a,
        )

        # Config-only change alters both tree_digest and config_digest -> distinct cache_key.
        (tmp_path / "sdkconfig.defaults").write_text("CONFIG_ESP_TASK_WDT_TIMEOUT_S=15\n")
        tree_b, cfg_b = compute_tree_and_config_digests(tmp_path)
        key_b, _ = compute_build_cache_key(
            idf_image=PINNED_IDF_IMAGES["esp32s3"],
            target="esp32s3",
            partition_layout="ab-4m-v1",
            source_tree_digest=tree_b,
            config_digest=cfg_b,
        )
        assert cfg_a != cfg_b
        assert key_a != key_b

    async def test_git_shallow_fetcher_strips_dot_git_before_compile(self, tmp_path: Path) -> None:
        origin = tmp_path / "origin"
        expected_sha = _init_git_repo(
            origin,
            {
                "CMakeLists.txt": "cmake_minimum_required(VERSION 3.16)\n",
                "main/main.c": "void app_main(void) {}\n",
                "sdkconfig.defaults": "CONFIG_FREERTOS_HZ=1000\n",
            },
            tag="v1.0.0",
        )
        dest = tmp_path / "checkout"
        logs: list[str] = []

        async def collect(chunk: str) -> None:
            logs.append(chunk)

        fetcher = GitShallowSourceFetcher(allow_local_paths=True)
        checkout = await fetcher.fetch(
            repo_url=str(origin),
            git_ref="v1.0.0",
            dest_dir=dest,
            on_log=collect,
        )
        assert checkout.commit_sha == expected_sha
        assert not (dest / ".git").exists(), ".git directory must be stripped before compile"
        assert (dest / "main" / "main.c").is_file()
        assert any("checked out" in line for line in logs)

    async def test_sandboxed_idf_container_runner_streams_logs_and_strips_ambient_env(
        self,
        tmp_path: Path,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        monkeypatch.setenv("FLEETFORGE_SUPER_SECRET", "must-never-leak-into-container")
        work_dir = tmp_path / "work"
        work_dir.mkdir()
        fake_docker = tmp_path / "fake-docker"
        fake_docker.write_text(
            "#!/bin/sh\n"
            'if [ -n "$FLEETFORGE_SUPER_SECRET" ]; then\n'
            "  exit 99\n"
            "fi\n"
            'echo "[idf] compiling firmware"\n'
            f"mkdir -p '{work_dir}/build'\n"
            f"printf '\\351\\000\\002\\040firmware-app-bytes' > '{work_dir}/build/app.bin'\n"
            f"printf 'boot' > '{work_dir}/build/bootloader.bin'\n",
            encoding="utf-8",
        )
        fake_docker.chmod(fake_docker.stat().st_mode | stat.S_IXUSR)

        lines: list[str] = []

        async def on_log(line: str) -> None:
            lines.append(line)

        runner = SandboxedIdfContainerRunner(docker_bin_override=str(fake_docker))
        app_bytes = await runner.compile_source(
            work_dir=work_dir,
            target="esp32c6",
            partition_layout="ab-4m-v1",
            idf_image=PINNED_IDF_IMAGES["esp32c6"],
            on_log=on_log,
        )
        assert app_bytes.startswith(b"\xe9\x00\x02\x20firmware-app-bytes")
        assert any("[idf] compiling firmware" in line for line in lines)


class _CountingBuildRunner:
    def __init__(self) -> None:
        self.calls = 0
        self._inner = DeterministicLocalBuildRunner()

    async def compile_source(self, **kwargs: Any) -> bytes:
        self.calls += 1
        return await self._inner.compile_source(**kwargs)


class TestBuildPipelineApiAndWorker:
    @pytest.fixture
    async def env(
        self,
        admin_app: FastAPI,
        engine: AsyncEngine,
        clean_tables: None,
        tmp_path: Path,
    ) -> dict[str, Any]:
        _ = clean_tables
        settings = settings_for_tests(
            artifact_signing_key=_PRIV_A,
            artifact_signing_public_keys=f"{_PUB_A};{_PUB_B}",
            sse_max_stream_s=5,
        )
        store = MemoryObjectStore()
        fetcher = GitShallowSourceFetcher(allow_local_paths=True)
        runner = _CountingBuildRunner()
        sessionmaker = async_sessionmaker(engine, expire_on_commit=False)

        admin_app.dependency_overrides[get_settings] = lambda: settings
        admin_app.dependency_overrides[get_object_store] = lambda: store
        admin_app.dependency_overrides[get_source_fetcher] = lambda: fetcher
        admin_app.dependency_overrides[get_build_runner] = lambda: runner

        token = await login_admin(admin_app)

        repo_dir = tmp_path / "fw-repo"
        commit_sha = _init_git_repo(
            repo_dir,
            {
                "CMakeLists.txt": "project(rover_fw)\n",
                "main/main.c": "void app_main(void) {}\n",
                "sdkconfig.defaults": "CONFIG_PARTITION_TABLE_CUSTOM=y\n",
            },
            tag="v2.1.0",
        )
        return {
            "app": admin_app,
            "token": token,
            "sessionmaker": sessionmaker,
            "settings": settings,
            "store": store,
            "fetcher": fetcher,
            "runner": runner,
            "repo_dir": repo_dir,
            "commit_sha": commit_sha,
        }

    async def test_queued_build_worker_produces_signed_provenance_artifact_and_hits_cache(
        self,
        env: dict[str, Any],
    ) -> None:
        headers = {"Authorization": f"Bearer {env['token']}"}
        async with client_for(env["app"], base_url="https://testserver") as client:
            # Queue first build at tag v2.1.0.
            resp = await client.post(
                "/v1/projects/default/builds",
                headers=headers,
                json={
                    "repo_url": str(env["repo_dir"]),
                    "git_ref": "v2.1.0",
                    "target": "esp32s3",
                    "version": "2.1.0",
                    "partition_layout": "ab-4m-v1",
                },
            )
            assert resp.status_code == 201, resp.text
            queued = resp.json()
            assert queued["status"] == "queued"
            build_id = queued["id"]

            # Drain queue via external worker `builder.run_once`.
            completed = await builder.run_once(
                env["sessionmaker"],
                env["store"],
                env["settings"],
                source_fetcher=env["fetcher"],
                build_runner=env["runner"],
            )
            assert len(completed) == 1
            assert completed[0].status == "succeeded"
            assert completed[0].cache_hit is False
            assert env["runner"].calls == 1
            sha256 = completed[0].artifact_sha256
            assert sha256 is not None

            # Verify blob in ObjectStore + ECDSA P-256 signature + DB provenance.
            stored_bytes = await env["store"].get(blob_key(sha256))
            assert len(stored_bytes) > 64
            async with env["sessionmaker"]() as s:
                art = await s.get(Artifact, sha256)
                assert art is not None
                assert art.sig is not None
                assert verify_digest(sha256, art.sig, [_PUB_A, _PUB_B])
                assert art.provenance is not None
                assert art.provenance["source_commit"] == env["commit_sha"]
                assert art.provenance["idf_image"] == PINNED_IDF_IMAGES["esp32s3"]
                assert art.provenance["cache_key"] == completed[0].cache_key
                cached_row = await s.scalar(
                    select(Build).where(Build.cache_key == completed[0].cache_key)
                )
                assert cached_row is not None

            # Artifact appears in GET /v1/artifact with commit & tag provenance.
            art_list = (await client.get("/v1/artifact", headers=headers)).json()["artifacts"]
            matching = [a for a in art_list if a["version"] == "2.1.0"]
            assert len(matching) == 1
            assert matching[0]["commit_sha"] == env["commit_sha"]
            assert matching[0]["tag"] == "v2.1.0"

            # Stream logs via SSE `/logs/stream` for the completed build -> delivers chunks + done:true.
            sse_resp = await client.get(
                f"/v1/projects/default/builds/{build_id}/logs/stream?poll_s=0.05",
                headers=headers,
            )
            assert sse_resp.status_code == 200
            data_lines = [
                line.removeprefix("data: ")
                for line in sse_resp.text.splitlines()
                if line.startswith("data: ")
            ]
            assert data_lines
            last_frame = json.loads(data_lines[-1])
            assert last_frame["done"] is True
            assert last_frame["status"] == "succeeded"
            assert "[3/3] Linking ESP-IDF app image" in last_frame["chunk"]

            # Second build of the identical tree + config hits Tier 1 cache without recompiling!
            second = await client.post(
                "/v1/projects/default/builds?run_inline=true",
                headers=headers,
                json={
                    "repo_url": str(env["repo_dir"]),
                    "git_ref": "v2.1.0",
                    "target": "esp32s3",
                    "version": "2.1.0-rebuild",
                    "partition_layout": "ab-4m-v1",
                },
            )
            assert second.status_code == 201, second.text
            second_body = second.json()
            assert second_body["status"] == "succeeded"
            assert second_body["cache_hit"] is True
            assert second_body["artifact_sha256"] == sha256
            assert env["runner"].calls == 1

    async def test_failed_build_transitions_and_supports_retry_and_cancel(
        self,
        env: dict[str, Any],
        tmp_path: Path,
    ) -> None:
        broken_repo = tmp_path / "broken-repo"
        _init_git_repo(
            broken_repo,
            {"main/main.c": "FF_BUILD_FAIL syntax error;\n"},
        )
        headers = {"Authorization": f"Bearer {env['token']}"}
        async with client_for(env["app"], base_url="https://testserver") as client:
            failed_resp = await client.post(
                "/v1/projects/default/builds?run_inline=true",
                headers=headers,
                json={
                    "repo_url": str(broken_repo),
                    "git_ref": "main",
                    "target": "esp32c3",
                    "version": "0.0.1-bad",
                },
            )
            assert failed_resp.status_code == 201
            failed_job = failed_resp.json()
            assert failed_job["status"] == "failed"
            assert "compile error" in (failed_job["error_detail"] or "")

            # Retry queues a fresh job; cancelling transitions it to `cancelled`.
            retried = await client.post(
                f"/v1/projects/default/builds/{failed_job['id']}/retry",
                headers=headers,
            )
            assert retried.status_code == 201
            retried_job = retried.json()
            assert retried_job["status"] == "queued"

            cancelled = await client.post(
                f"/v1/projects/default/builds/{retried_job['id']}/cancel",
                headers=headers,
            )
            assert cancelled.status_code == 200
            assert cancelled.json()["status"] == "cancelled"

            async with env["sessionmaker"]() as s:
                row = await s.get(BuildJob, retried_job["id"])
                assert row is not None
                assert row.status == "cancelled"
                assert BuildError is not None

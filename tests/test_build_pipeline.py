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
    GitShallowSourceFetcher,
    SandboxedIdfContainerRunner,
    build_sandboxed_docker_cmd,
    compute_build_cache_key,
    compute_tree_and_config_digests,
    create_build_runner,
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
from tests.test_doubles import StubBuildRunner

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
            project_id="00000000-0000-4000-8000-000000000001",
            idf_image=PINNED_IDF_IMAGES["esp32s3"],
            target="esp32s3",
            partition_layout="ab-4m-v1",
            source_tree_digest=tree_a,
            config_digest=cfg_a,
        )
        key_other_project, _ = compute_build_cache_key(
            project_id="00000000-0000-4000-8000-000000000002",
            idf_image=PINNED_IDF_IMAGES["esp32s3"],
            target="esp32s3",
            partition_layout="ab-4m-v1",
            source_tree_digest=tree_a,
            config_digest=cfg_a,
        )
        assert key_a != key_other_project

        # Config-only change alters both tree_digest and config_digest -> distinct cache_key.
        (tmp_path / "sdkconfig.defaults").write_text("CONFIG_ESP_TASK_WDT_TIMEOUT_S=15\n")
        tree_b, cfg_b = compute_tree_and_config_digests(tmp_path)
        key_b, _ = compute_build_cache_key(
            project_id="00000000-0000-4000-8000-000000000001",
            idf_image=PINNED_IDF_IMAGES["esp32s3"],
            target="esp32s3",
            partition_layout="ab-4m-v1",
            source_tree_digest=tree_b,
            config_digest=cfg_b,
        )
        assert cfg_a != cfg_b
        assert key_a != key_b

    async def test_tree_digest_covers_directories_named_build(self, tmp_path: Path) -> None:
        """A-25: `components/build/*` is covered by the tree digest, committed `build/` is stripped."""
        origin = tmp_path / "origin"
        _init_git_repo(
            origin,
            {
                "main/main.c": "void app_main(void) {}\n",
                "components/build/drv.c": "int drv_v(void) { return 1; }\n",
                "build/00_planted.bin": "not-real-firmware",
            },
        )
        fetcher = GitShallowSourceFetcher(allow_local_paths=True)
        dest_a = tmp_path / "checkout_a"
        co_a = await fetcher.fetch(repo_url=str(origin), git_ref="main", dest_dir=dest_a)
        assert not (dest_a / "build").exists(), "committed top-level build/ must be deleted"
        assert (dest_a / "components" / "build" / "drv.c").is_file()

        git_bin = shutil.which("git") or "/usr/bin/git"
        env = {
            "PATH": os.environ.get("PATH", "/usr/bin:/bin"),
            "GIT_AUTHOR_NAME": "Test",
            "GIT_AUTHOR_EMAIL": "test@example.com",
            "GIT_COMMITTER_NAME": "Test",
            "GIT_COMMITTER_EMAIL": "test@example.com",
        }
        (origin / "components" / "build" / "drv.c").write_text(
            "int drv_v(void) { return 2; }\n", encoding="utf-8"
        )
        subprocess.run([git_bin, "-C", str(origin), "add", "."], check=True, env=env)  # noqa: S603
        subprocess.run(  # noqa: S603
            [git_bin, "-C", str(origin), "commit", "-q", "-m", "update build component"],
            check=True,
            env=env,
        )
        dest_b = tmp_path / "checkout_b"
        co_b = await fetcher.fetch(repo_url=str(origin), git_ref="main", dest_dir=dest_b)
        assert co_a.source_tree_digest != co_b.source_tree_digest

    async def test_git_ref_beginning_with_a_dash_is_rejected(self, tmp_path: Path) -> None:
        """A-23: leading `-`, whitespace, control chars, and `..` are rejected before Git runs."""
        from fleetforge.api.schemas_v2 import BuildJobCreate

        origin = tmp_path / "origin"
        _init_git_repo(origin, {"main/main.c": "void app_main(void) {}\n"})
        fetcher = GitShallowSourceFetcher(allow_local_paths=True)

        bad_refs = [
            "--orphan=unborn",
            "--pathspec-from-file=/etc/passwd",
            "-b",
            "main branch",
            "main\n--upload-pack=id",
            "main..HEAD",
            "main'spoof",
        ]
        for bad_ref in bad_refs:
            with pytest.raises(ValueError):
                BuildJobCreate(
                    repo_url="https://github.com/example/fw",
                    git_ref=bad_ref,
                    target="esp32s3",
                    version="1.0.0",
                )
            with pytest.raises(BuildError):
                await fetcher.fetch(
                    repo_url=str(origin),
                    git_ref=bad_ref,
                    dest_dir=tmp_path / f"dst_{abs(hash(bad_ref))}",
                )

    async def test_fetch_does_not_follow_redirects(
        self,
        tmp_path: Path,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        """A-23: `git fetch` refuses HTTP 302 redirects and never contacts the redirect target."""
        import datetime as dt
        import http.server
        import ssl
        import threading

        from cryptography import x509
        from cryptography.hazmat.primitives import hashes, serialization
        from cryptography.hazmat.primitives.asymmetric import rsa
        from cryptography.x509.oid import NameOID

        import fleetforge.builds as builds_mod

        key = rsa.generate_private_key(public_exponent=65537, key_size=2048)
        subject = x509.Name([x509.NameAttribute(NameOID.COMMON_NAME, "127.0.0.1")])
        now = dt.datetime.now(dt.UTC)
        cert = (
            x509.CertificateBuilder()
            .subject_name(subject)
            .issuer_name(subject)
            .public_key(key.public_key())
            .serial_number(x509.random_serial_number())
            .not_valid_before(now - dt.timedelta(minutes=5))
            .not_valid_after(now + dt.timedelta(hours=1))
            .add_extension(
                x509.SubjectAlternativeName(
                    [x509.IPAddress(__import__("ipaddress").IPv4Address("127.0.0.1"))]
                ),
                critical=False,
            )
            .sign(key, hashes.SHA256())
        )
        cert_file = tmp_path / "cert.pem"
        key_file = tmp_path / "key.pem"
        cert_file.write_bytes(cert.public_bytes(serialization.Encoding.PEM))
        key_file.write_bytes(
            key.private_bytes(
                serialization.Encoding.PEM,
                serialization.PrivateFormat.TraditionalOpenSSL,
                serialization.NoEncryption(),
            )
        )

        hits: list[str] = []

        class RedirectHandler(http.server.BaseHTTPRequestHandler):
            def do_GET(self) -> None:  # noqa: N802
                hits.append(self.path)
                if self.path.startswith("/redirect.git"):
                    self.send_response(302)
                    self.send_header(
                        "Location",
                        f"https://127.0.0.1:{server.server_port}/secret-target.git/info/refs",
                    )
                    self.end_headers()
                    return
                self.send_response(200)
                self.end_headers()

            def log_message(self, _fmt: str, *_args: object) -> None:
                pass

        server = http.server.ThreadingHTTPServer(("127.0.0.1", 0), RedirectHandler)
        ssl_ctx = ssl.SSLContext(ssl.PROTOCOL_TLS_SERVER)
        ssl_ctx.load_cert_chain(certfile=str(cert_file), keyfile=str(key_file))
        server.socket = ssl_ctx.wrap_socket(server.socket, server_side=True)
        thread = threading.Thread(target=server.serve_forever, daemon=True)
        thread.start()

        async def _allow_loopback_in_test(url: str, *, field_name: str = "url") -> str:
            _ = field_name
            return url

        monkeypatch.setattr(builds_mod, "sanitize_git_repo_url", lambda u, **_kw: u)
        monkeypatch.setattr(
            builds_mod, "resolve_and_validate_public_https_url", _allow_loopback_in_test
        )

        try:
            fetcher = GitShallowSourceFetcher(timeout_s=10.0, ca_bundle=cert_file)
            with pytest.raises(BuildError):
                await fetcher.fetch(
                    repo_url=f"https://127.0.0.1:{server.server_port}/redirect.git",
                    git_ref="main",
                    dest_dir=tmp_path / "redirect-dst",
                )
        finally:
            server.shutdown()
            server.server_close()
            thread.join(timeout=2.0)

        assert any(p.startswith("/redirect.git") for p in hits)
        assert not any("secret-target" in p for p in hits), f"redirect was followed: {hits}"

    async def test_dns_ssrf_and_otel_client_hardening(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """A-23: DNS resolution blocks private/loopback IPs and OTel client disables env proxy."""
        import socket

        from fleetforge.ingestor.otel import (
            OtelExportBatch,
            close_otel_client,
            get_otel_client,
            send_otel_batch,
        )
        from fleetforge.url_safety import resolve_and_validate_public_https_url

        loop = __import__("asyncio").get_running_loop()

        async def _private_dns(
            _host: object, _port: object, **_kw: object
        ) -> list[tuple[Any, ...]]:
            return [(socket.AF_INET, socket.SOCK_STREAM, 6, "", ("169.254.169.254", 443))]

        monkeypatch.setattr(loop, "getaddrinfo", _private_dns)
        with pytest.raises(ValueError, match="loopback, private, link-local"):
            await resolve_and_validate_public_https_url("https://rebind.example.com/hook")

        batch = OtelExportBatch(
            signal="logs",
            device_id="a4cf12b3de01",
            url="https://rebind.example.com/v1/logs",
            body={"resourceLogs": []},
            headers={"Content-Type": "application/json"},
        )
        assert await send_otel_batch(batch) is False

        client = get_otel_client()
        assert client._trust_env is False
        assert client.follow_redirects is False
        await close_otel_client()

        async def _public_dns(_host: object, _port: object, **_kw: object) -> list[tuple[Any, ...]]:
            return [(socket.AF_INET, socket.SOCK_STREAM, 6, "", ("93.184.216.34", 443))]

        monkeypatch.setattr(loop, "getaddrinfo", _public_dns)
        assert (
            await resolve_and_validate_public_https_url("https://example.com/hook")
            == "https://example.com/hook"
        )

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
            f"printf 'planted-decoy' > '{work_dir}/build/00_planted.bin'\n"
            f"printf '\\351\\000\\002\\040firmware-app-bytes' > '{work_dir}/build/my_app.bin'\n"
            f"printf 'boot' > '{work_dir}/build/bootloader.bin'\n"
            f"printf '{{\"app_bin\": \"my_app.bin\"}}' > '{work_dir}/build/project_description.json'\n",
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

    async def test_container_runner_rejects_missing_or_escaping_project_description(
        self, tmp_path: Path
    ) -> None:
        """A-25: fail closed when `project_description.json` is absent or escapes `build/`."""
        work_dir = tmp_path / "work"
        work_dir.mkdir()
        (tmp_path / "outside.bin").write_bytes(b"secret-outside")

        for label, script_body in (
            (
                "missing",
                f"mkdir -p '{work_dir}/build'\nprintf 'planted' > '{work_dir}/build/00_planted.bin'\n",
            ),
            (
                "escape",
                f"mkdir -p '{work_dir}/build'\n"
                f"printf '{{\"app_bin\": \"../../outside.bin\"}}' > '{work_dir}/build/project_description.json'\n",
            ),
            (
                "build-symlink",
                f"ln -s '{tmp_path}' '{work_dir}/build'\n",
            ),
            (
                "app-symlink",
                f"mkdir -p '{work_dir}/build'\n"
                f"ln -s '{tmp_path}/outside.bin' '{work_dir}/build/link.bin'\n"
                f"printf '{{\"app_bin\": \"link.bin\"}}' > '{work_dir}/build/project_description.json'\n",
            ),
        ):
            fake = tmp_path / f"fake-{label}"
            fake.write_text("#!/bin/sh\n" + script_body, encoding="utf-8")
            fake.chmod(fake.stat().st_mode | stat.S_IXUSR)
            runner = SandboxedIdfContainerRunner(docker_bin_override=str(fake))
            with pytest.raises(BuildError):
                await runner.compile_source(
                    work_dir=work_dir,
                    target="esp32c6",
                    partition_layout="ab-4m-v1",
                    idf_image=PINNED_IDF_IMAGES["esp32c6"],
                )


class _CountingBuildRunner:
    def __init__(self) -> None:
        self.calls = 0
        self._inner = StubBuildRunner()

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

    async def test_auto_backend_without_docker_fails_the_job(
        self,
        env: dict[str, Any],
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        """A-24: `create_build_runner('auto')` without Docker must fail the job, never fabricate a binary."""
        monkeypatch.delenv("FF_BUILD_BACKEND", raising=False)
        real_which = shutil.which
        monkeypatch.setattr(
            shutil,
            "which",
            lambda name: None if name == "docker" else real_which(name),
        )

        with pytest.raises(ValueError, match="unsupported build backend"):
            create_build_runner("local")

        auto_runner = create_build_runner("auto")
        assert isinstance(auto_runner, SandboxedIdfContainerRunner)

        headers = {"Authorization": f"Bearer {env['token']}"}
        async with client_for(env["app"], base_url="https://testserver") as client:
            queued = await client.post(
                "/v1/projects/default/builds",
                headers=headers,
                json={
                    "repo_url": str(env["repo_dir"]),
                    "git_ref": "v2.1.0",
                    "target": "esp32s3",
                    "version": "2.2.0-nodocker",
                    "partition_layout": "ab-4m-v1",
                },
            )
            assert queued.status_code == 201, queued.text

            completed = await builder.run_once(
                env["sessionmaker"],
                env["store"],
                env["settings"],
                source_fetcher=env["fetcher"],
                build_runner=auto_runner,
            )
            assert len(completed) == 1
            assert completed[0].status == "failed"
            assert "docker" in (completed[0].error_detail or "").lower()
            assert completed[0].artifact_sha256 is None

    async def test_scoped_token_matrix_on_build_routes(
        self,
        env: dict[str, Any],
    ) -> None:
        """A-26: CI tokens (`write:artifacts`, `deploy`) can queue/cancel/retry builds while
        `read`, `enroll`, `viewer`, and cross-project tokens receive 403."""
        super_headers = {"Authorization": f"Bearer {env['token']}"}
        async with client_for(env["app"], base_url="https://testserver") as client:
            r_other = await client.post(
                "/v1/projects",
                headers=super_headers,
                json={"slug": "other-builds", "name": "Other Builds"},
            )
            assert r_other.status_code == 201

            r_viewer = await client.post(
                "/v1/users",
                headers=super_headers,
                json={
                    "username": "buildviewer",
                    "password": "viewer-password-1234",
                    "project": "default",
                    "role": "viewer",
                },
            )
            assert r_viewer.status_code == 201
            viewer_login = await client.post(
                "/v1/auth/login",
                json={"username": "buildviewer", "password": "viewer-password-1234"},
            )
            viewer_headers = {"Authorization": f"Bearer {viewer_login.cookies['ff_session']}"}

            async def _mint(
                name: str, scopes: list[str], project: str = "default"
            ) -> dict[str, str]:
                resp = await client.post(
                    "/v1/auth/tokens",
                    headers=super_headers,
                    json={"name": name, "project": project, "scopes": scopes},
                )
                assert resp.status_code == 201, resp.text
                return {"Authorization": f"Bearer {resp.json()['token']}"}

            read_hdrs = await _mint("b-read", ["read"])
            enroll_hdrs = await _mint("b-enroll", ["enroll"])
            wa_hdrs = await _mint("b-write-art", ["write:artifacts"])
            deploy_hdrs = await _mint("b-deploy", ["deploy"])
            wrong_proj_hdrs = await _mint(
                "b-wrong-proj", ["write:artifacts", "deploy"], "other-builds"
            )

            payload = {
                "repo_url": str(env["repo_dir"]),
                "git_ref": "v2.1.0",
                "target": "esp32s3",
                "version": "3.0.0",
            }

            # Forbidden on POST /builds: viewer, read, enroll, wrong project.
            for forbidden in (viewer_headers, read_hdrs, enroll_hdrs, wrong_proj_hdrs):
                r = await client.post(
                    "/v1/projects/default/builds", headers=forbidden, json=payload
                )
                assert r.status_code == 403, f"expected 403, got {r.status_code}: {r.text}"

            # Allowed on POST /builds, cancel, and retry: `write:artifacts` and `deploy` tokens!
            for idx, (allowed_hdrs, ver) in enumerate(((wa_hdrs, "3.0.1"), (deploy_hdrs, "3.0.2"))):
                r_create = await client.post(
                    "/v1/projects/default/builds",
                    headers=allowed_hdrs,
                    json={**payload, "version": ver},
                )
                assert r_create.status_code == 201, r_create.text
                bid = r_create.json()["id"]

                # Viewer and read-only tokens can read list/detail, but cannot cancel.
                assert (
                    await client.get(f"/v1/projects/default/builds/{bid}", headers=viewer_headers)
                ).status_code == 200
                assert (
                    await client.get(f"/v1/projects/default/builds/{bid}", headers=read_hdrs)
                ).status_code == 200
                assert (
                    await client.post(
                        f"/v1/projects/default/builds/{bid}/cancel", headers=viewer_headers
                    )
                ).status_code == 403
                assert (
                    await client.post(
                        f"/v1/projects/default/builds/{bid}/cancel", headers=read_hdrs
                    )
                ).status_code == 403

                r_cancel = await client.post(
                    f"/v1/projects/default/builds/{bid}/cancel", headers=allowed_hdrs
                )
                assert r_cancel.status_code == 200, r_cancel.text

                assert (
                    await client.post(
                        f"/v1/projects/default/builds/{bid}/retry", headers=viewer_headers
                    )
                ).status_code == 403
                r_retry = await client.post(
                    f"/v1/projects/default/builds/{bid}/retry", headers=allowed_hdrs
                )
                assert r_retry.status_code == 201, (idx, r_retry.text)

    async def test_rebuilding_an_existing_version_with_new_bytes_fails_the_job(
        self,
        env: dict[str, Any],
    ) -> None:
        """A-25: rebuilding an existing `(project, target, version)` with different bytes fails
        the build job and keeps `artifact_versions` pointing at the original digest; also
        verifies Git tag vs branch classification (`vendor-fix` is a branch, not a tag) and
        enqueue URL stripping."""
        headers = {"Authorization": f"Bearer {env['token']}"}
        repo_dir: Path = env["repo_dir"]
        git_bin = shutil.which("git") or "/usr/bin/git"
        git_env = {
            "PATH": os.environ.get("PATH", "/usr/bin:/bin"),
            "GIT_AUTHOR_NAME": "Test",
            "GIT_AUTHOR_EMAIL": "test@example.com",
            "GIT_COMMITTER_NAME": "Test",
            "GIT_COMMITTER_EMAIL": "test@example.com",
        }

        async with client_for(env["app"], base_url="https://testserver") as client:
            # First build succeeds and binds 2.1.0 -> sha1.
            first = await client.post(
                "/v1/projects/default/builds?run_inline=true",
                headers=headers,
                json={
                    "repo_url": str(repo_dir),
                    "git_ref": "v2.1.0",
                    "target": "esp32s3",
                    "version": "2.1.0",
                    "partition_layout": "ab-4m-v1",
                },
            )
            assert first.status_code == 201, first.text
            first_body = first.json()
            assert first_body["status"] == "succeeded"
            original_sha = first_body["artifact_sha256"]
            assert original_sha is not None

            # Create branch `vendor-fix` (starts with 'v', NOT a tag) with new bytes.
            subprocess.run(  # noqa: S603
                [git_bin, "-C", str(repo_dir), "checkout", "-q", "-b", "vendor-fix"],
                check=True,
                env=git_env,
            )
            (repo_dir / "main" / "main.c").write_text(
                "void app_main(void) { /* updated */ }\n", encoding="utf-8"
            )
            subprocess.run([git_bin, "-C", str(repo_dir), "add", "."], check=True, env=git_env)  # noqa: S603
            subprocess.run(  # noqa: S603
                [git_bin, "-C", str(repo_dir), "commit", "-q", "-m", "vendor fix"],
                check=True,
                env=git_env,
            )

            # Rebuilding the existing 2.1.0 version with new bytes MUST fail!
            conflict = await client.post(
                "/v1/projects/default/builds?run_inline=true",
                headers=headers,
                json={
                    "repo_url": str(repo_dir),
                    "git_ref": "vendor-fix",
                    "target": "esp32s3",
                    "version": "2.1.0",
                    "partition_layout": "ab-4m-v1",
                },
            )
            assert conflict.status_code == 201, conflict.text
            conflict_body = conflict.json()
            assert conflict_body["status"] == "failed"
            assert conflict_body["artifact_sha256"] is None
            assert "already names artifact" in (conflict_body["error_detail"] or "")

            # Under a fresh version label, `vendor-fix` is recorded as a branch, never a tag.
            fresh = await client.post(
                "/v1/projects/default/builds?run_inline=true",
                headers=headers,
                json={
                    "repo_url": str(repo_dir),
                    "git_ref": "vendor-fix",
                    "target": "esp32s3",
                    "version": "2.1.1",
                    "partition_layout": "ab-4m-v1",
                },
            )
            assert fresh.status_code == 201, fresh.text
            assert fresh.json()["status"] == "succeeded"

            arts = (await client.get("/v1/artifact", headers=headers)).json()["artifacts"]
            by_ver = {a["version"]: a for a in arts}
            assert by_ver["2.1.0"]["sha256"] == original_sha
            assert by_ver["2.1.0"]["tag"] == "v2.1.0"
            assert by_ver["2.1.0"].get("branch") is None
            assert by_ver["2.1.1"]["branch"] == "vendor-fix"
            assert by_ver["2.1.1"].get("tag") is None

            # Annotated tag not starting with 'v' IS classified as a tag by Git.
            subprocess.run(  # noqa: S603
                [git_bin, "-C", str(repo_dir), "tag", "-a", "release-2", "-m", "rel 2"],
                check=True,
                env=git_env,
            )
            tagged = await client.post(
                "/v1/projects/default/builds?run_inline=true",
                headers=headers,
                json={
                    "repo_url": str(repo_dir),
                    "git_ref": "release-2",
                    "target": "esp32s3",
                    "version": "2.1.1-rel2",
                    "partition_layout": "ab-4m-v1",
                },
            )
            assert tagged.status_code == 201, tagged.text
            assert tagged.json()["status"] == "succeeded"
            arts2 = (await client.get("/v1/artifact", headers=headers)).json()["artifacts"]
            rel2 = next(a for a in arts2 if a["version"] == "2.1.1-rel2")
            assert rel2["tag"] == "release-2"
            assert rel2.get("branch") is None

            # A-23 enqueue checks: bad git_ref -> 422, strip `?token=...`, reject private/credential URLs.
            dash_ref = await client.post(
                "/v1/projects/default/builds",
                headers=headers,
                json={
                    "repo_url": "https://github.com/example/fw.git",
                    "git_ref": "--orphan=unborn",
                    "target": "esp32s3",
                    "version": "2.1.2-badref",
                },
            )
            assert dash_ref.status_code == 422, dash_ref.text
            stripped = await client.post(
                "/v1/projects/default/builds",
                headers=headers,
                json={
                    "repo_url": "https://github.com/example/fw.git?token=secret123#frag",
                    "git_ref": "main",
                    "target": "esp32s3",
                    "version": "2.1.2",
                },
            )
            assert stripped.status_code == 201, stripped.text
            assert stripped.json()["repo_url"] == "https://github.com/example/fw.git"
            assert "secret123" not in stripped.text

            for bad_url in (
                "http://github.com/example/fw.git",
                "https://127.0.0.1/fw.git",
                "https://" + "user:secret" + "@github.com/example/fw.git",
            ):
                bad = await client.post(
                    "/v1/projects/default/builds",
                    headers=headers,
                    json={
                        "repo_url": bad_url,
                        "git_ref": "main",
                        "target": "esp32s3",
                        "version": "2.1.3",
                    },
                )
                assert bad.status_code == 400, (bad_url, bad.text)

    async def test_merged_image_from_build_runner_is_rejected(
        self,
        env: dict[str, Any],
    ) -> None:
        """A-25: `execute_build_job` runs `detect_merged` and fails if a merged full-flash image is produced."""
        merged_bytes = (b"\xff" * 0x1000) + b"\xe9\x00\x02\x20" + (b"\x00" * 64)

        class _MergedBuildRunner:
            async def compile_source(self, **_kwargs: Any) -> bytes:
                return merged_bytes

        env["app"].dependency_overrides[get_build_runner] = lambda: _MergedBuildRunner()
        headers = {"Authorization": f"Bearer {env['token']}"}
        async with client_for(env["app"], base_url="https://testserver") as client:
            resp = await client.post(
                "/v1/projects/default/builds?run_inline=true",
                headers=headers,
                json={
                    "repo_url": str(env["repo_dir"]),
                    "git_ref": "v2.1.0",
                    "target": "esp32s3",
                    "version": "9.9.9-merged",
                },
            )
            assert resp.status_code == 201, resp.text
            body = resp.json()
            assert body["status"] == "failed"
            assert body["artifact_sha256"] is None
            assert "merged" in (body["error_detail"] or "").lower()

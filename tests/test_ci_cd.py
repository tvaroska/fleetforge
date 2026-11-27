"""CI/CD tracking rules, gate skip-guard contracts, Cloud Build E2E wiring, and repo hygiene (`T-10`)."""

from __future__ import annotations

import json
import os
import re
import subprocess
import sys
from pathlib import Path
from typing import Any

from scripts.check_pytest_skips import validate_pytest_output

from fleetforge.db.models import Project
from fleetforge.rollout import matches_project_tracking


class TestProjectTrackingMatcher:
    def _project(self, **overrides: Any) -> Project:
        values: dict[str, Any] = {
            "slug": "eink-frame",
            "name": "E-Ink Frame",
            "repo_url": "https://github.com/tvaroska/eink-frame.git",
            "track_mode": "branch",
            "track_ref": "main",
            "auto_deploy_target": "dev",
        }
        values.update(overrides)
        return Project(**values)

    def test_branch_tracking_matches_exact_branch_and_normalizes_repo_url(self) -> None:
        proj = self._project(track_mode="branch", track_ref="main")
        assert matches_project_tracking(
            proj,
            repo_url="https://github.com/tvaroska/eink-frame/",
            branch="main",
            commit_sha="a1b2c3d4",
            tag=None,
            test_passed=True,
        )
        assert not matches_project_tracking(
            proj,
            repo_url="https://github.com/tvaroska/eink-frame",
            branch="feature/weather",
            commit_sha="a1b2c3d4",
            tag=None,
            test_passed=True,
        )

    def test_failed_tests_never_auto_deploy(self) -> None:
        proj = self._project(track_mode="branch", track_ref="main")
        assert not matches_project_tracking(
            proj,
            repo_url=None,
            branch="main",
            commit_sha="a1b2c3d4",
            tag=None,
            test_passed=False,
        )

    def test_tag_glob_tracking_matches_wildcard_and_exact_tags(self) -> None:
        proj = self._project(track_mode="tag", track_ref="v*")
        assert matches_project_tracking(
            proj,
            repo_url=None,
            branch=None,
            commit_sha="a1b2c3d4",
            tag="v1.2.0",
            test_passed=True,
        )
        assert not matches_project_tracking(
            proj,
            repo_url=None,
            branch="main",
            commit_sha="a1b2c3d4",
            tag="nightly-20261020",
            test_passed=True,
        )

    def test_commit_tracking_matches_full_sha_or_prefix(self) -> None:
        proj = self._project(track_mode="commit", track_ref="a1b2c3d")
        assert matches_project_tracking(
            proj,
            repo_url=None,
            branch="main",
            commit_sha="a1b2c3d4e5f60718293a4b5c6d7e8f90a1b2c3d4",
            tag=None,
            test_passed=None,
        )
        assert not matches_project_tracking(
            proj,
            repo_url=None,
            branch="main",
            commit_sha="deadbeef0000",
            tag=None,
            test_passed=True,
        )

    def test_off_mode_or_off_target_never_matches(self) -> None:
        assert not matches_project_tracking(
            self._project(track_mode="off", track_ref="main"),
            repo_url=None,
            branch="main",
            commit_sha="a1b2c3d",
            tag=None,
            test_passed=True,
        )
        assert not matches_project_tracking(
            self._project(track_mode="branch", track_ref="main", auto_deploy_target="off"),
            repo_url=None,
            branch="main",
            commit_sha="a1b2c3d",
            tag=None,
            test_passed=True,
        )

    def test_different_repo_url_is_rejected_when_project_pins_repo(self) -> None:
        proj = self._project(repo_url="https://github.com/tvaroska/eink-frame")
        assert not matches_project_tracking(
            proj,
            repo_url="https://github.com/other/repo",
            branch="main",
            commit_sha="a1b2c3d",
            tag=None,
            test_passed=True,
        )


class TestTrack2ShowcaseAndUxHygiene:
    """Track 2 showcase repo and documentation hygiene checks (`DOC-1..DOC-5`, `IMPL-1`)."""

    def test_license_and_manifest_metadata_present(self) -> None:
        repo_root = Path(__file__).resolve().parent.parent
        license_file = repo_root / "LICENSE"
        assert license_file.is_file()
        assert "MIT License" in license_file.read_text(encoding="utf-8")

        pyproject = (repo_root / "pyproject.toml").read_text(encoding="utf-8")
        assert 'license = { text = "MIT" }' in pyproject

        pkg_json = json.loads((repo_root / "frontend" / "package.json").read_text(encoding="utf-8"))
        assert pkg_json.get("license") == "MIT"

        lib_json = json.loads(
            (repo_root / "agent" / "components" / "fleetforge" / "library.json").read_text(
                encoding="utf-8"
            )
        )
        assert lib_json.get("license") == "MIT"

        idf_yml = (
            repo_root / "agent" / "components" / "fleetforge" / "idf_component.yml"
        ).read_text(encoding="utf-8")
        assert 'license: "MIT"' in idf_yml

    def test_critical_md_paths_exist(self) -> None:
        """DOC-5: Every concrete repository file path named in CRITICAL.md must exist."""
        repo_root = Path(__file__).resolve().parent.parent
        critical_text = (repo_root / "CRITICAL.md").read_text(encoding="utf-8")
        tokens = re.findall(r"`([A-Za-z0-9_./-]+\.[A-Za-z0-9_]+)`", critical_text)
        checked = 0
        for tok in tokens:
            if "/" not in tok or tok.startswith("/"):
                continue
            target = repo_root / tok
            assert target.exists(), f"CRITICAL.md references missing path: {tok}"
            checked += 1
        assert checked >= 15

    def test_docs_have_no_unshipped_claims_or_stale_todo_links(self) -> None:
        """DOC-2, DOC-4: README, releases.md, and library README match shipped code."""
        repo_root = Path(__file__).resolve().parent.parent
        readme = (repo_root / "README.md").read_text(encoding="utf-8")
        assert "v0.9.2" in readme
        assert "(TODO.md)" not in readme

        releases = (repo_root / "docs" / "releases.md").read_text(encoding="utf-8")
        for unshipped in (
            "actions/fleetforge-push/action.yml",
            ".gitlab/fleetforge-push.yml",
            ".gitea/workflows/fleetforge-push.yml",
            "cloudbuild/fleetforge-push.yaml",
            "auto_on_tag",
            "require_ci_tests",
        ):
            assert unshipped not in releases, unshipped

        lib_readme = (repo_root / "agent" / "components" / "fleetforge" / "README.md").read_text(
            encoding="utf-8"
        )
        assert "FluxTray.jar" not in lib_readme

    def test_cloudbuild_ci_pipeline_present_and_no_github_workflows(self) -> None:
        """IMPL-1: CI runs on GCP Cloud Build (`cloudbuild.yaml`) with no `.github/workflows`."""
        repo_root = Path(__file__).resolve().parent.parent
        cb = repo_root / "cloudbuild.yaml"
        assert cb.is_file()
        cb_text = cb.read_text(encoding="utf-8")
        assert "python-lint-typecheck-and-unit-tests" in cb_text
        assert "frontend-typecheck-and-tests" in cb_text
        assert "docker-build-images" in cb_text
        assert "mirror-to-ghcr" in cb_text
        assert "ghcr-push-token" in cb_text
        assert not (repo_root / ".github" / "workflows").exists()

    def test_frontend_ux4_ux5_ux6_confirmations_queued_breakdown_and_copy_snippet(self) -> None:
        """UX-4, UX-5, UX-6, UX-14: Destructive confirmations, queued vs skipped, and copy snippet."""
        repo_root = Path(__file__).resolve().parent.parent
        builds_ui = "\n".join(
            (repo_root / "frontend" / "src" / name).read_text(encoding="utf-8")
            for name in ("Dashboard.tsx", "UploadedBuilds.tsx")
        )
        frontend_tsx = "\n".join(
            p.read_text(encoding="utf-8")
            for p in sorted((repo_root / "frontend" / "src").rglob("*.tsx"))
            if not p.name.endswith(".test.tsx")
        )
        assert 'data-testid="confirm-bulk-action"' in frontend_tsx
        assert "Confirm deploy all" in frontend_tsx
        assert "Confirm cancel rollout" in frontend_tsx
        assert "queued for next wave" in frontend_tsx
        assert 'data-testid="copy-cli-snippet"' in frontend_tsx
        assert "RULE E301" not in builds_ui
        assert 'data-testid="confirm-delete-secret"' in frontend_tsx
        assert 'data-testid="confirm-delete-group"' in frontend_tsx

    def test_improv_ble_rpc_result_identify_and_disconnect_advertising(self) -> None:
        repo_root = Path(__file__).resolve().parent.parent
        agent_src = "\n".join(
            p.read_text(encoding="utf-8")
            for p in sorted(
                (repo_root / "agent" / "components" / "fleetforge" / "src").glob("*.[ch]")
            )
        )
        assert "#define IMPROV_ERR_UNKNOWN_RPC 0x02" in agent_src
        assert "#define IMPROV_CMD_IDENTIFY 0x02" in agent_src
        assert "improv_set_rpc_result(IMPROV_CMD_WIFI_SETTINGS)" in agent_src
        assert "ble_gatts_chr_updated(s_improv_rpc_res_val_handle)" in agent_src
        assert "BLE_GAP_EVENT_DISCONNECT" in agent_src
        assert "improv_ble_gap_event" in agent_src


def test_gate_fails_when_database_tests_cannot_run() -> None:
    """T-17 / T-20 / T-26: unreachable Postgres fails fast once, CI refuses FF_ALLOW_DB_SKIP=1,
    and the skip-allowlist guard catches any DB skip or unallowlisted skip."""
    repo_root = Path(__file__).resolve().parents[1]
    gate_script = repo_root / "scripts" / "gate.sh"
    ci_gate_script = repo_root / "scripts" / "ci_gate.sh"

    assert "TestCiCdDatabaseAndApi" not in gate_script.read_text(encoding="utf-8")
    assert "TestCiCdDatabaseAndApi" not in ci_gate_script.read_text(encoding="utf-8")
    assert "gate.sh" in ci_gate_script.read_text(encoding="utf-8")

    base_env = dict(os.environ)
    base_env.pop("FF_ALLOW_DB_SKIP", None)
    base_env.pop("CI", None)
    base_env["TEST_DATABASE_URL"] = "postgresql+asyncpg://unused:unused@127.0.0.1:1/fleetforge_test"

    proc_no_db = subprocess.run(  # noqa: S603
        [
            sys.executable,
            "-m",
            "pytest",
            "-q",
            "-rs",
            "-p",
            "no:cacheprovider",
            "tests/test_schema.py",
        ],
        cwd=repo_root,
        env=base_env,
        capture_output=True,
        text=True,
        timeout=30,
        check=False,
    )
    combined_no_db = proc_no_db.stdout + proc_no_db.stderr
    assert proc_no_db.returncode == 1, combined_no_db
    assert "Cannot reach PostgreSQL" in combined_no_db
    assert "TEST_DATABASE_URL" in combined_no_db
    assert "btvaroska" in combined_no_db

    ci_env = {**base_env, "CI": "1", "FF_ALLOW_DB_SKIP": "1"}
    proc_ci_skip = subprocess.run(  # noqa: S603
        ["bash", str(gate_script)],  # noqa: S607
        cwd=repo_root,
        env=ci_env,
        capture_output=True,
        text=True,
        timeout=30,
        check=False,
    )
    assert proc_ci_skip.returncode == 1
    assert "FF_ALLOW_DB_SKIP=1 is forbidden when CI is set" in (
        proc_ci_skip.stdout + proc_ci_skip.stderr
    )

    mutated_skip_out = (
        "SKIPPED [1] tests/conftest.py:165: Cannot reach PostgreSQL at localhost:5433\n"
        "1855 passed, 1 skipped in 4.20s\n"
    )
    violations = validate_pytest_output(mutated_skip_out, {})
    assert violations, "expected skip allowlist to reject a PostgreSQL skip"
    assert any("Cannot reach PostgreSQL" in v for v in violations)

    clean_out = (
        "SKIPPED [2] tests/test_agent_power_and_size.py:403: no bundle in agent/dist — run `just agent-build-all`\n"
        "SKIPPED [6] tests/test_artifact_download_minio.py: MinIO tests opt-in via FF_RUN_MINIO_TESTS=1 (target http://localhost:9000)\n"
        "SKIPPED [4] tests/test_object_store_minio.py: MinIO tests opt-in via FF_RUN_MINIO_TESTS=1 (target http://localhost:9000)\n"
        "SKIPPED [5] tests/contracts/test_object_store_contract.py: MinIO tests opt-in via FF_RUN_MINIO_TESTS=1 (target http://localhost:9000)\n"
        "2625 passed, 17 skipped in 12.34s\n"
    )
    assert validate_pytest_output(clean_out, {}) == []
    overflow_out = (
        "SKIPPED [6] tests/contracts/test_object_store_contract.py: "
        "MinIO tests opt-in via FF_RUN_MINIO_TESTS=1 (target http://localhost:9000)\n"
        "2625 passed, 6 skipped in 12.34s\n"
    )
    assert any("MinIO contract skips" in err for err in validate_pytest_output(overflow_out, {}))
    assert validate_pytest_output(clean_out, {"FF_RUN_MINIO_TESTS": "1"}) != []
    mixed_bad_out = (
        "SKIPPED [1] tests/conftest.py:165: Cannot reach PostgreSQL at localhost:5433\n"
        "SKIPPED [1] tests/test_something.py:12: accidental skip\n"
        "100 passed, 2 skipped in 1.20s\n"
    )
    assert validate_pytest_output(mixed_bad_out, {"FF_ALLOW_DB_SKIP": "1"}) != []
    assert validate_pytest_output(mutated_skip_out, {"FF_ALLOW_DB_SKIP": "1"}) == []


def test_cloudbuild_wires_fullstack_and_playwright_e2e_with_pinned_version() -> None:
    """T-28 & T-29: Cloud Build runs full-stack OTA E2E and all 4 Playwright suites with matched pin."""
    repo_root = Path(__file__).resolve().parents[1]
    cloudbuild = (repo_root / "cloudbuild.yaml").read_text(encoding="utf-8")
    dockerfile_e2e = (repo_root / "docker" / "Dockerfile.e2e").read_text(encoding="utf-8")
    pkg = json.loads((repo_root / "frontend" / "package.json").read_text(encoding="utf-8"))
    pw_ver = pkg["config"]["playwrightVersion"]

    assert f"ARG PLAYWRIGHT_VERSION={pw_ver}" in dockerfile_e2e
    assert "FROM mcr.microsoft.com/playwright:v${PLAYWRIGHT_VERSION}-noble" in dockerfile_e2e
    assert "docker/Dockerfile.e2e" in cloudbuild
    for needle in (
        "id: verify-fullstack-and-playwright-e2e",
        "eclipse-mosquitto:2.0.22",
        "fleetforge.ingestor.main",
        "scripts/selftest_window_e2e.py",
        "scripts/update-flow-e2e.mjs",
        "scripts/fleet-health-e2e.mjs",
        "scripts/device-log-e2e.mjs",
        "scripts/commands-e2e.mjs",
        "- verify-fullstack-and-playwright-e2e",
    ):
        assert needle in cloudbuild, f"missing {needle!r} in cloudbuild.yaml"

    for script_name in (
        "update-flow-e2e.mjs",
        "fleet-health-e2e.mjs",
        "device-log-e2e.mjs",
        "commands-e2e.mjs",
    ):
        src = (repo_root / "frontend" / "scripts" / script_name).read_text(encoding="utf-8")
        assert "process.env.PLAYWRIGHT_MODULE || 'playwright'" in src
        assert "FF_SIM_PYTHON" in src
        assert "console-tabs" in src


def test_offline_wheel_packages_contracts_and_imports_under_isolated_python(
    tmp_path: Path,
) -> None:
    """`D-10`/`D-29`/`R-095`: cold-cache `--no-build-isolation` wheel build bundles `contracts/*.json`."""
    import shutil
    import sysconfig
    import tomllib
    import zipfile

    uv_bin = shutil.which("uv")
    assert uv_bin is not None
    repo_root = Path(__file__).resolve().parents[1]
    pyproject_data = tomllib.loads((repo_root / "pyproject.toml").read_text(encoding="utf-8"))
    dev_deps = pyproject_data.get("dependency-groups", {}).get("dev", [])
    assert any(str(dep).startswith("hatchling") for dep in dev_deps), (
        "pyproject.toml [dependency-groups].dev must pin hatchling so "
        "`uv sync --frozen --no-install-project` in Cloud Build can build wheels offline"
    )

    out_dir = tmp_path / "dist"
    cold_cache = tmp_path / "cold-uv-cache"
    cold_cache.mkdir()
    proc_build = subprocess.run(  # noqa: S603
        [
            uv_bin,
            "build",
            "--wheel",
            "--offline",
            "--no-config",
            "--no-build-isolation",
            "--python",
            sys.executable,
            "--cache-dir",
            str(cold_cache),
            "--out-dir",
            str(out_dir),
        ],
        cwd=repo_root,
        capture_output=True,
        text=True,
        check=False,
    )
    assert proc_build.returncode == 0, proc_build.stdout + proc_build.stderr
    wheels = list(out_dir.glob("fleetforge-*.whl"))
    assert len(wheels) == 1
    unpacked = tmp_path / "unpacked"
    with zipfile.ZipFile(wheels[0]) as zf:
        names = set(zf.namelist())
        assert "fleetforge/contracts/capabilities.json" in names
        assert "fleetforge/contracts/wire_vectors.json" in names
        zf.extractall(unpacked)

    purelib = sysconfig.get_path("purelib")
    platlib = sysconfig.get_path("platlib")
    probe = (
        "import sys; from pathlib import Path; "
        f"sys.path[:0] = [{str(unpacked)!r}, {purelib!r}, {platlib!r}]; "
        "import fleetforge, fleetforge.capabilities as caps; "
        "from fleetforge.contracts import load_wire_vectors_contract; "
        f"assert Path(fleetforge.__file__).resolve().is_relative_to(Path({str(unpacked)!r})); "
        "assert 'ota' in caps.BASE_CAPABILITIES; "
        "assert 'vectors' in load_wire_vectors_contract()"
    )
    proc_probe = subprocess.run(  # noqa: S603
        [sys.executable, "-I", "-S", "-c", probe],
        cwd=tmp_path,
        capture_output=True,
        text=True,
        check=False,
    )
    assert proc_probe.returncode == 0, proc_probe.stdout + proc_probe.stderr

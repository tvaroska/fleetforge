"""Tests for agent/tools/check_bundles_fresh.py.

Pure unit tests over a throwaway git repo in tmp_path; no docker, no ESP-IDF, no bundle
bytes. Importing via importlib.util because agent/ is not a package and must not become one.
"""

import importlib.util
import json
import subprocess
from pathlib import Path

import pytest

# Load the script as a module
script_path = Path(__file__).parent.parent / "agent" / "tools" / "check_bundles_fresh.py"
spec = importlib.util.spec_from_file_location("check_bundles_fresh", script_path)
assert spec and spec.loader
cbf = importlib.util.module_from_spec(spec)
spec.loader.exec_module(cbf)


def _git(repo: Path, *args: str, skip_config: bool = False) -> subprocess.CompletedProcess[str]:
    """Run a git command with signing disabled."""
    cmd = ["git"]
    if not skip_config:
        cmd.extend(
            ["-c", "user.name=test", "-c", "user.email=test@test", "-c", "commit.gpgsign=false"]
        )
    cmd.extend(["-C", str(repo), *args])
    return subprocess.run(cmd, capture_output=True, text=True, check=True)  # noqa: S603,S607


def _write_manifest(bundle_dir: Path, source_commit: str, built_at: str, target: str) -> None:
    """Write a minimal manifest.json with the given provenance."""
    manifest = {
        "target": target,
        "source_commit": source_commit,
        "built_at": built_at,
    }
    bundle_dir.mkdir(parents=True, exist_ok=True)
    (bundle_dir / "manifest.json").write_text(json.dumps(manifest, indent=2) + "\n")


@pytest.fixture
def git_repo(tmp_path: Path) -> Path:
    """Create a throwaway git repo with agent/ sources."""
    repo = tmp_path / "repo"
    repo.mkdir()

    # Initialize repo
    _git(repo, "init", skip_config=True)
    _git(repo, "config", "init.defaultBranch", "main")

    # Create agent/ structure
    agent_dir = repo / "agent"
    agent_main = agent_dir / "main"
    agent_main.mkdir(parents=True)
    (agent_main / "agent_main.c").write_text("// initial\n")

    agent_dist = agent_dir / "dist"
    agent_dist.mkdir()
    (agent_dist / ".gitkeep").write_text("")

    # Initial commit
    _git(repo, "add", ".")
    _git(repo, "commit", "-m", "initial commit")

    return repo


def test_bundle_built_at_head_passes(git_repo: Path) -> None:
    """A bundle built at HEAD is fresh."""
    head = _git(git_repo, "rev-parse", "HEAD").stdout.strip()
    bundle_dir = git_repo / "agent" / "dist" / "esp32"
    _write_manifest(bundle_dir, head, "2026-09-11T12:00:00Z", "esp32")

    result = cbf.main(["--repo", str(git_repo), str(bundle_dir)])
    assert result == 0


def test_bundle_predates_agent_main_is_stale(git_repo: Path) -> None:
    """A bundle built before a change to agent/main/ is STALE."""
    # Capture HEAD before the change
    old_head = _git(git_repo, "rev-parse", "HEAD").stdout.strip()
    bundle_dir = git_repo / "agent" / "dist" / "esp32"
    _write_manifest(bundle_dir, old_head, "2026-09-11T12:00:00Z", "esp32")

    # Make a change under agent/main/
    (git_repo / "agent" / "main" / "agent_main.c").write_text("// updated\n")
    _git(git_repo, "add", "agent/main/agent_main.c")
    _git(git_repo, "commit", "-m", "update agent")

    # Bundle should now be stale
    result = cbf.main(["--repo", str(git_repo), str(bundle_dir)])
    assert result == 1


def test_bundle_after_non_agent_change_is_fresh(git_repo: Path) -> None:
    """A bundle is fresh even after a commit outside agent/ (proves the pathspec)."""
    # Build a bundle at HEAD
    head = _git(git_repo, "rev-parse", "HEAD").stdout.strip()
    bundle_dir = git_repo / "agent" / "dist" / "esp32"
    _write_manifest(bundle_dir, head, "2026-09-11T12:00:00Z", "esp32")

    # Make a change outside agent/
    (git_repo / "README.md").write_text("# readme\n")
    _git(git_repo, "add", "README.md")
    _git(git_repo, "commit", "-m", "add readme")

    # Bundle should still be fresh
    result = cbf.main(["--repo", str(git_repo), str(bundle_dir)])
    assert result == 0


def test_bundle_after_agent_dist_change_is_fresh(git_repo: Path) -> None:
    """A bundle is fresh even after a change under agent/dist/ (proves the exclude)."""
    # Build a bundle at HEAD
    head = _git(git_repo, "rev-parse", "HEAD").stdout.strip()
    bundle_dir = git_repo / "agent" / "dist" / "esp32"
    _write_manifest(bundle_dir, head, "2026-09-11T12:00:00Z", "esp32")

    # Make a change under agent/dist/
    (git_repo / "agent" / "dist" / "scratch.txt").write_text("scratch\n")
    _git(git_repo, "add", "agent/dist/scratch.txt")
    _git(git_repo, "commit", "-m", "update dist")

    # Bundle should still be fresh
    result = cbf.main(["--repo", str(git_repo), str(bundle_dir)])
    assert result == 0


def test_unknown_source_commit_is_untraceable(git_repo: Path) -> None:
    """A bundle with source_commit='unknown' is UNTRACEABLE."""
    bundle_dir = git_repo / "agent" / "dist" / "esp32"
    _write_manifest(bundle_dir, "unknown", "2026-09-11T12:00:00Z", "esp32")

    result = cbf.main(["--repo", str(git_repo), str(bundle_dir)])
    assert result == 1


def test_random_sha_is_untraceable(git_repo: Path) -> None:
    """A bundle with a random 40-hex sha that's not in the repo is UNTRACEABLE."""
    bundle_dir = git_repo / "agent" / "dist" / "esp32"
    _write_manifest(bundle_dir, "a" * 40, "2026-09-11T12:00:00Z", "esp32")

    result = cbf.main(["--repo", str(git_repo), str(bundle_dir)])
    assert result == 1


def test_missing_manifest_is_not_built(git_repo: Path) -> None:
    """A bundle directory with no manifest.json is NOT BUILT."""
    bundle_dir = git_repo / "agent" / "dist" / "esp32"
    bundle_dir.mkdir(parents=True, exist_ok=True)
    # No manifest

    result = cbf.main(["--repo", str(git_repo), str(bundle_dir)])
    assert result == 1


def test_uncommitted_agent_change_is_dirty(git_repo: Path) -> None:
    """An uncommitted edit to agent/main/ reports DIRTY SOURCES."""
    head = _git(git_repo, "rev-parse", "HEAD").stdout.strip()
    bundle_dir = git_repo / "agent" / "dist" / "esp32"
    _write_manifest(bundle_dir, head, "2026-09-11T12:00:00Z", "esp32")

    # Uncommitted change
    (git_repo / "agent" / "main" / "agent_main.c").write_text("// dirty\n")

    result = cbf.main(["--repo", str(git_repo), str(bundle_dir)])
    assert result == 1


def test_two_bundles_one_stale_reports_both(git_repo: Path) -> None:
    """When checking multiple bundles, one bad doesn't stop the rest from being reported."""
    # Capture old HEAD
    old_head = _git(git_repo, "rev-parse", "HEAD").stdout.strip()

    # Commit a change
    (git_repo / "agent" / "main" / "agent_main.c").write_text("// updated\n")
    _git(git_repo, "add", "agent/main/agent_main.c")
    _git(git_repo, "commit", "-m", "update agent")
    new_head = _git(git_repo, "rev-parse", "HEAD").stdout.strip()

    # Bundle 1: stale
    bundle1 = git_repo / "agent" / "dist" / "esp32"
    _write_manifest(bundle1, old_head, "2026-09-11T12:00:00Z", "esp32")

    # Bundle 2: fresh
    bundle2 = git_repo / "agent" / "dist" / "esp32c3"
    _write_manifest(bundle2, new_head, "2026-09-11T13:00:00Z", "esp32c3")

    result = cbf.main(["--repo", str(git_repo), str(bundle1), str(bundle2)])
    assert result == 1


def test_no_vacuity_on_dirty_tree_check(git_repo: Path, capsys) -> None:
    """Removing the dirty-tree check would let an uncommitted change pass — check that it fails."""
    head = _git(git_repo, "rev-parse", "HEAD").stdout.strip()
    bundle_dir = git_repo / "agent" / "dist" / "esp32"
    _write_manifest(bundle_dir, head, "2026-09-11T12:00:00Z", "esp32")

    # Uncommitted change
    (git_repo / "agent" / "main" / "agent_main.c").write_text("// dirty\n")

    result = cbf.main(["--repo", str(git_repo), str(bundle_dir)])
    assert result == 1
    captured = capsys.readouterr()
    assert "uncommitted changes" in captured.err


def test_no_vacuity_on_stale_check(git_repo: Path, capsys) -> None:
    """Removing the ancestry check would let a stale bundle pass — check that it fails."""
    old_head = _git(git_repo, "rev-parse", "HEAD").stdout.strip()
    bundle_dir = git_repo / "agent" / "dist" / "esp32"
    _write_manifest(bundle_dir, old_head, "2026-09-11T12:00:00Z", "esp32")

    # Commit a change
    (git_repo / "agent" / "main" / "agent_main.c").write_text("// updated\n")
    _git(git_repo, "add", "agent/main/agent_main.c")
    _git(git_repo, "commit", "-m", "update agent")

    result = cbf.main(["--repo", str(git_repo), str(bundle_dir)])
    assert result == 1
    captured = capsys.readouterr()
    assert "STALE" in captured.err

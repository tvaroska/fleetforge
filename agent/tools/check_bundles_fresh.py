"""Check that agent bundles were built from a commit containing the newest agent sources.

The release-time half of S0-infra-2: bundles in `agent/dist/` are proven correct by
`verify_bundle.py`, but a correct bundle can still be wrong to ship. v0.3.0 shipped three
targets predating S0-fw-1 (the stage reporter) — not because anyone changed the code and
forgot to rebuild, but because nobody checked. This script makes a stale bundle unshippable.

Why git provenance, not mtime: `git checkout`, `git pull` and branch switches rewrite
source mtimes with no content change; a clone sets them all to clone time. A check that
fires on a correct tree is the check people delete. Git ancestry answers the same question
("does this bundle contain the newest agent source change?") and cannot be wrong about it.

Standard library only, and linted by ruff but not mypy (agent/tools/ has no venv, and the
ESP-IDF image has no type stubs).
"""

from __future__ import annotations

import argparse
import json
import subprocess
import sys
from pathlib import Path

# design/decisions/infrastructure-agent-bundles-are-artifacts.md: when bundles move to the
# object store, this script will check the one being uploaded. The pathspec is the Docker
# build context that agent/.dockerignore defines (`COPY . /project` minus `dist/`).
SOURCE_PATHSPEC = ("agent", ":(exclude)agent/dist")


class StaleBundle(RuntimeError):
    """A bundle was built from a commit that predates the agent sources."""


def _git(repo: Path, *args: str) -> subprocess.CompletedProcess[str]:
    """Run a git command in the given repo. Never shell out."""
    # noqa: S603,S607 - fixed argv, no shell
    return subprocess.run(
        ["git", "-C", str(repo), *args], capture_output=True, text=True, check=False
    )


def last_source_commit(repo: Path) -> str:
    """The newest commit that touched the agent sources.

    Returns the full 40-hex sha. Raises if git is not available or the repo has no .git.
    """
    result = _git(repo, "log", "-1", "--format=%H", "--", *SOURCE_PATHSPEC)
    if result.returncode != 0:
        raise StaleBundle(
            f"check_bundles_fresh: not a git repository ({repo}); provenance cannot be checked"
        )
    return result.stdout.strip()


def dirty_sources(repo: Path) -> list[str]:
    """List of uncommitted changes under the agent source pathspec.

    Returns paths relative to the repo root. An empty list means the tree is clean.
    """
    result = _git(repo, "status", "--porcelain", "--", *SOURCE_PATHSPEC)
    if result.returncode != 0:
        return []
    lines = [line.strip() for line in result.stdout.splitlines() if line.strip()]
    # status --porcelain: XY path, we want the path
    return [line.split(maxsplit=1)[1] if " " in line else "" for line in lines if line]


def commit_exists(repo: Path, sha: str) -> bool:
    """True if the given sha is a commit object in this repo."""
    result = _git(repo, "rev-parse", "--verify", "--quiet", f"{sha}^{{commit}}")
    return result.returncode == 0


def contains(repo: Path, ancestor: str, sha: str) -> bool:
    """True if ancestor is reachable from sha (i.e. sha contains ancestor's changes)."""
    result = _git(repo, "merge-base", "--is-ancestor", ancestor, sha)
    return result.returncode == 0


def check_bundle(repo: Path, bundle_dir: Path, newest: str) -> str:
    """Check one bundle. Returns source_commit on success; raises on failure.

    Raises StaleBundle with a message naming the target, the verdict, the bundle's
    commit + built_at, the newest source commit, and the remedy.
    """
    manifest_path = bundle_dir / "manifest.json"
    if not manifest_path.is_file():
        target = bundle_dir.name
        raise StaleBundle(
            f"{target:<8} NOT BUILT — no manifest in {bundle_dir}\n"
            f"         Run: just agent-build {target}"
        )

    try:
        manifest = json.loads(manifest_path.read_text())
        source_commit = manifest.get("source_commit", "")
        built_at = manifest.get("built_at", "unknown")
        target = manifest.get("target", bundle_dir.name)
    except (json.JSONDecodeError, OSError) as exc:
        target = bundle_dir.name
        raise StaleBundle(
            f"{target:<8} NOT BUILT — cannot read manifest: {exc}\n"
            f"         Run: just agent-build {target}"
        ) from exc

    if not source_commit or source_commit == "unknown":
        raise StaleBundle(
            f"{target:<8} UNTRACEABLE — manifest has no source_commit\n"
            f"         Built: {built_at}\n"
            f"         Run: just agent-build {target}"
        )

    if not commit_exists(repo, source_commit):
        raise StaleBundle(
            f"{target:<8} UNTRACEABLE — source_commit {source_commit[:7]} "
            f"is not a commit in this repo\n"
            f"         Built: {built_at}\n"
            f"         Run: just agent-build {target}"
        )

    if not contains(repo, newest, source_commit):
        # Get a one-line summary of the newest commit
        summary_result = _git(repo, "log", "-1", "--format=%h %ad %s", "--date=short", newest)
        newest_summary = (
            summary_result.stdout.strip() if summary_result.returncode == 0 else newest[:7]
        )

        raise StaleBundle(
            f"{target:<8} STALE — built from {source_commit[:7]} ({built_at})\n"
            f"         Newest agent source: {newest_summary}\n"
            f"         Run: just agent-build {target}"
        )

    # Success
    return source_commit


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        description=__doc__,
        formatter_class=argparse.RawDescriptionHelpFormatter,
    )
    parser.add_argument(
        "--repo",
        type=Path,
        default=Path.cwd(),
        help="Path to the git repository (default: current directory)",
    )
    parser.add_argument(
        "bundle_dirs",
        metavar="BUNDLE_DIR",
        type=Path,
        nargs="+",
        help="One or more bundle directories to check",
    )
    args = parser.parse_args(argv)

    repo: Path = args.repo
    bundle_dirs: list[Path] = args.bundle_dirs

    try:
        newest = last_source_commit(repo)
    except StaleBundle as exc:
        print(str(exc), file=sys.stderr)
        return 1

    # Check for dirty sources ONCE, not per bundle
    dirty = dirty_sources(repo)
    if dirty:
        sample = dirty[:5]
        more = f", … ({len(dirty)} total)" if len(dirty) > 5 else ""
        print(
            f"agent sources have uncommitted changes ({', '.join(sample)}{more}); "
            "a bundle records HEAD, not what was compiled — commit before releasing.",
            file=sys.stderr,
        )
        return 1

    failures = []
    for bundle_dir in bundle_dirs:
        try:
            source_commit = check_bundle(repo, bundle_dir, newest)
            # Get built_at for the success message
            manifest = json.loads((bundle_dir / "manifest.json").read_text())
            built_at = manifest.get("built_at", "unknown")
            target = manifest.get("target", bundle_dir.name)
            print(f"{target:<8} fresh — built from {source_commit[:7]} {built_at}")
        except StaleBundle as exc:
            failures.append(str(exc))

    if failures:
        print("", file=sys.stderr)
        for failure in failures:
            print(failure, file=sys.stderr)
        return 1

    return 0


if __name__ == "__main__":
    raise SystemExit(main())

"""Repository hygiene & link integrity tests (`ADR-005` / `CRITICAL.md`).

Verifies that `fleetforge` remains 100% portable and free of:
1. Internal codenames, internal shortlinks, Buganizer IDs, and internal monorepo paths.
2. Customer names or cross-workspace local paths.
3. Hardcoded machine home directory paths or `file:///` links.
4. Sibling-repo relative Markdown links (`../<sibling>/`) or `.github/workflows/` CI files.
5. Broken relative Markdown links across all tracked documentation.
"""

from __future__ import annotations

import re
import shutil
import subprocess
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parent.parent

EXCLUDED_DIRS = {
    "bucket",
    "dist",
    "build",
    "out",
    "__pycache__",
    ".git",
    ".pytest_cache",
    ".venv",
    "node_modules",
    ".ruff_cache",
    ".mypy_cache",
}

FORBIDDEN_PATTERNS: list[tuple[str, re.Pattern[str]]] = [
    (
        "Hardcoded local home directory path",
        re.compile(r"/usr/local/google/home|\.c\.googlers\.com"),
    ),
    (
        "File URI scheme",
        re.compile(r"file:///(?:usr/local/google|Users/|home/|[A-Za-z]:/)"),
    ),
    ("Internal Buganizer ID", re.compile(r"\bb/\d{6,}\b")),
    (
        "Internal shortlink",
        re.compile(r"\bgo/(?!mod\b|sum\b|work\b|version\b|test\b|build\b|run\b)[a-zA-Z0-9_-]{3,}"),
    ),
    ("Internal monorepo path", re.compile(r"//depot/" + r"google3|\bgoogle3\b")),
    (
        "Cross-workspace local path",
        re.compile(r"~/" + r"customers|~/notes|~/ideas|~/robotics|~/projects"),
    ),
    ("Deprecated model family string", re.compile(r"gemini-2\.5-")),
]


def _iter_tracked_text_files() -> list[Path]:
    git_bin = shutil.which("git") or "/usr/bin/git"
    listed = subprocess.run(  # noqa: S603
        [git_bin, "-C", str(REPO_ROOT), "ls-files", "-z"],
        check=True,
        capture_output=True,
    ).stdout.decode("utf-8", errors="replace")
    files: list[Path] = []
    for rel in sorted(filter(None, listed.split("\0"))):
        path = REPO_ROOT / rel
        if not path.is_file():
            continue
        rel_parts = Path(rel).parts
        if any(p in EXCLUDED_DIRS for p in rel_parts):
            continue
        if rel_parts == ("tests", "test_repo_hygiene.py"):
            continue
        if path.suffix.lower() in {
            ".stl",
            ".step",
            ".3mf",
            ".pdf",
            ".png",
            ".jpg",
            ".webp",
            ".mp4",
            ".bin",
            ".elf",
            ".uf2",
            ".woff2",
            ".ico",
            ".lock",
        }:
            continue
        files.append(path)
    return files


def test_zero_github_workflows() -> None:
    gh_workflows = REPO_ROOT / ".github" / "workflows"
    assert not gh_workflows.exists(), (
        "CI/CD must use cloudbuild.yaml only (zero .github/workflows/)"
    )


def test_claude_md_symlink_and_spine_caps() -> None:
    agents_md = REPO_ROOT / "AGENTS.md"
    claude_md = REPO_ROOT / "CLAUDE.md"
    critical_md = REPO_ROOT / "CRITICAL.md"
    decisions_md = REPO_ROOT / "DECISIONS.md"
    readme_md = REPO_ROOT / "README.md"

    assert agents_md.is_file()
    assert agents_md.stat().st_size < 24_000, (
        "AGENTS.md must stay strictly below 24 KB truncation cap"
    )
    assert critical_md.is_file()
    assert critical_md.stat().st_size <= 5_000, (
        f"CRITICAL.md must stay <= 5,000 bytes (got {critical_md.stat().st_size})"
    )
    assert readme_md.is_file()
    assert decisions_md.is_file()
    assert decisions_md.stat().st_size <= 25_000, "DECISIONS.md must stay <= 25 KB"
    assert claude_md.is_symlink(), "CLAUDE.md must be a symlink to AGENTS.md"
    assert claude_md.readlink().as_posix() == "AGENTS.md"


def test_uv_lock_uses_public_pypi_registry_only() -> None:
    """T-23: `uv.lock` must only reference public PyPI registries and distribution hosts."""
    uv_lock = REPO_ROOT / "uv.lock"
    assert uv_lock.is_file(), "uv.lock is required"
    text = uv_lock.read_text(encoding="utf-8")
    registries = set(re.findall(r'registry\s*=\s*"([^"]+)"', text))
    assert registries == {"https://pypi.org/simple"}, (
        f"uv.lock must only reference https://pypi.org/simple, found: {sorted(registries)}"
    )
    urls = set(re.findall(r'url\s*=\s*"([^"]+)"', text))
    non_pypi = sorted(u for u in urls if not u.startswith("https://files.pythonhosted.org/"))
    assert not non_pypi, f"uv.lock contains non-PyPI package URLs: {non_pypi[:5]}"


def test_zero_internal_customer_or_machine_path_leaks() -> None:
    violations: list[str] = []
    for fpath in _iter_tracked_text_files():
        try:
            text = fpath.read_text(encoding="utf-8")
        except UnicodeDecodeError:
            continue
        rel = fpath.relative_to(REPO_ROOT)
        for label, pattern in FORBIDDEN_PATTERNS:
            match = pattern.search(text)
            if match:
                violations.append(f"{rel}: {label} ({match.group(0)!r})")
    assert not violations, "Repository hygiene violations found:\n" + "\n".join(violations)


def _github_heading_slugs(markdown_text: str) -> set[str]:
    """Extract GitHub-style `#anchor` slugs from ATX headings (skipping fenced code blocks)."""
    slugs: set[str] = set()
    counts: dict[str, int] = {}
    in_fence = False
    for line in markdown_text.splitlines():
        stripped = line.lstrip()
        if stripped.startswith(("```", "~~~")):
            in_fence = not in_fence
            continue
        if in_fence:
            continue
        m = re.match(r"^#{1,6}\s+(.+?)\s*#*\s*$", stripped)
        if not m:
            continue
        heading = m.group(1)
        # Strip inline markdown links `[label](url)` -> `label`, inline code/emphasis markers.
        # Keep literal `_` (GFM retains `_` in snake_case headings via `\w`).
        heading = re.sub(r"\[([^\]]+)\]\([^)]+\)", r"\1", heading)
        heading = heading.replace("`", "").replace("*", "")
        slug = re.sub(r"[^\w\s-]", "", heading.lower(), flags=re.UNICODE)
        slug = re.sub(r"\s+", "-", slug.strip())
        if not slug:
            continue
        idx = counts.get(slug, 0)
        counts[slug] = idx + 1
        slugs.add(slug if idx == 0 else f"{slug}-{idx}")
    return slugs


def _iter_markdown_links(markdown_text: str) -> list[str]:
    """Return link targets outside fenced code blocks."""
    md_link_re = re.compile(r"\[[^\]\n]+\]\(([^)\s]+)\)")
    links: list[str] = []
    in_fence = False
    for line in markdown_text.splitlines():
        if line.lstrip().startswith(("```", "~~~")):
            in_fence = not in_fence
            continue
        if in_fence:
            continue
        for match in md_link_re.finditer(line):
            links.append(match.group(1))
    return links


def test_github_heading_slugs_and_anchor_detection() -> None:
    """T-12 positive control: snake_case headings keep `_`, duplicates suffix, code blocks skipped."""
    doc = (
        "# `foo_bar` Setup\n"
        "## Overview\n"
        "## Overview\n"
        "```md\n"
        "# Ignored Heading\n"
        "[ignored](missing.md#nope)\n"
        "```\n"
        "[ok](#foo_bar-setup)\n"
    )
    slugs = _github_heading_slugs(doc)
    assert "foo_bar-setup" in slugs
    assert "overview" in slugs
    assert "overview-1" in slugs
    assert "ignored-heading" not in slugs
    assert _iter_markdown_links(doc) == ["#foo_bar-setup"]


def test_all_relative_markdown_links_resolve_within_repo() -> None:
    broken: list[str] = []
    repo_resolved = REPO_ROOT.resolve()
    slug_cache: dict[Path, set[str]] = {}
    for md_file in sorted(REPO_ROOT.rglob("*.md")):
        rel_parts = md_file.relative_to(REPO_ROOT).parts
        if any(p.startswith(".") or p in EXCLUDED_DIRS for p in rel_parts):
            continue
        text = md_file.read_text(encoding="utf-8")
        for raw_link in _iter_markdown_links(text):
            if raw_link.startswith(("http://", "https://", "mailto:")):
                continue
            raw_target, _, raw_anchor = raw_link.partition("#")
            if not raw_target:
                target = md_file.resolve()
            else:
                target = (md_file.parent / raw_target.replace("%20", " ")).resolve()
            if not target.is_relative_to(repo_resolved):
                broken.append(f"{md_file.relative_to(REPO_ROOT)}: sibling escape link ({raw_link})")
                continue
            if not target.exists():
                broken.append(f"{md_file.relative_to(REPO_ROOT)}: {raw_link}")
                continue
            if raw_anchor and target.is_file() and target.suffix.lower() == ".md":
                if target not in slug_cache:
                    slug_cache[target] = _github_heading_slugs(
                        target.read_text(encoding="utf-8", errors="ignore")
                    )
                if raw_anchor.lower() not in slug_cache[target]:
                    broken.append(
                        f"{md_file.relative_to(REPO_ROOT)}: missing anchor #{raw_anchor} in "
                        f"{target.relative_to(REPO_ROOT)}"
                    )
    assert not broken, "Broken or escaping markdown links found:\n" + "\n".join(broken)

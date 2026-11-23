"""ESP-IDF board firmware starter template loading and rendering."""

from __future__ import annotations

import io
import re
import tarfile
import urllib.error
import urllib.parse
import urllib.request
from pathlib import Path

from fleetforge import __version__
from fleetforge.cli_client import DEFAULT_PUBLIC_REPO_URL


def _sanitize_app_name(raw: str) -> str:
    cleaned = re.sub(r"[^a-z0-9_]+", "_", raw.strip().lower()).strip("_")
    if not cleaned or not cleaned[0].isalpha():
        cleaned = f"app_{cleaned}" if cleaned else "board_app"
    return cleaned[:32]


def _sanitize_project_slug(raw: str) -> str:
    cleaned = re.sub(r"[^a-z0-9-]+", "-", raw.strip().lower()).strip("-")
    if not cleaned or not cleaned[0].isalnum():
        cleaned = f"proj-{cleaned}" if cleaned else "default"
    return cleaned[:64]


def _github_tarball_url(repo_url: str, ref: str) -> str:
    """Convert a GitHub repo URL (`https://github.com/owner/repo[.git]`) into a codeload tarball URL."""
    stripped = repo_url.rstrip("/")
    if stripped.endswith(".git"):
        stripped = stripped[:-4]
    parsed = urllib.parse.urlparse(stripped)
    path_parts = [p for p in parsed.path.strip("/").split("/") if p]
    if parsed.netloc in ("github.com", "www.github.com") and len(path_parts) >= 2:
        owner, repo = path_parts[0], path_parts[1]
        return f"https://codeload.github.com/{owner}/{repo}/tar.gz/{ref}"
    return f"{stripped}/archive/{ref}.tar.gz"


def _download_tarball(repo_url: str, ref: str) -> bytes:
    candidates = [ref, "main"] if ref != "main" else [ref]
    last_err: Exception | None = None
    for candidate_ref in candidates:
        url = _github_tarball_url(repo_url, candidate_ref)
        req = urllib.request.Request(url, headers={"User-Agent": f"fleetforge-cli/{__version__}"})
        try:
            with urllib.request.urlopen(req, timeout=30) as resp:
                return bytes(resp.read())
        except (urllib.error.URLError, OSError) as exc:
            last_err = exc
    raise RuntimeError(f"failed to fetch template from {repo_url} ({ref}): {last_err}")


def _fetch_remote_template_files(
    repo_url: str, ref: str, template_subdir: str = "templates/esp-idf"
) -> dict[str, bytes]:
    """Download `templates/esp-idf/` from the public GitHub repository tarball."""
    raw_tar = _download_tarball(repo_url, ref)
    files: dict[str, bytes] = {}
    prefix_needle = f"/{template_subdir.strip('/')}/"
    with tarfile.open(fileobj=io.BytesIO(raw_tar), mode="r:gz") as tf:
        for member in tf.getmembers():
            if not member.isfile():
                continue
            norm_name = "/" + member.name.lstrip("/")
            idx = norm_name.find(prefix_needle)
            if idx == -1:
                continue
            rel_path = norm_name[idx + len(prefix_needle) :]
            if not rel_path or ".." in Path(rel_path).parts:
                continue
            extracted = tf.extractfile(member)
            if extracted is not None:
                files[rel_path] = extracted.read()

    if not files:
        raise RuntimeError(f"template '{template_subdir}' not found in archive from {repo_url}")
    return files


def load_template_files(
    *,
    repo_url: str = DEFAULT_PUBLIC_REPO_URL,
    ref: str = f"v{__version__}",
    prefer_remote: bool = False,
) -> dict[str, bytes]:
    """Load `templates/esp-idf/` from the local checkout or public GitHub repository."""
    local_dir = Path(__file__).resolve().parents[2] / "templates" / "esp-idf"
    if not prefer_remote and local_dir.is_dir():
        files: dict[str, bytes] = {}
        for path in sorted(local_dir.rglob("*")):
            if path.is_file():
                rel = path.relative_to(local_dir).as_posix()
                files[rel] = path.read_bytes()
        if files:
            return files
    return _fetch_remote_template_files(repo_url, ref)


def render_template_project(
    dest_dir: Path,
    *,
    app_name: str,
    project_slug: str,
    target: str,
    fleetforge_url: str,
    fleetforge_ref: str,
    repo_url: str = DEFAULT_PUBLIC_REPO_URL,
    prefer_remote: bool = False,
    force: bool = False,
) -> list[Path]:
    """Scaffold an ESP-IDF board repository into `dest_dir` and return written file paths."""
    if dest_dir.exists() and any(dest_dir.iterdir()) and not force:
        raise FileExistsError(
            f"destination directory '{dest_dir}' is not empty (pass --force to overwrite)"
        )

    raw_files = load_template_files(
        repo_url=repo_url,
        ref=fleetforge_ref,
        prefer_remote=prefer_remote,
    )
    replacements = {
        "{{APP_NAME}}": app_name,
        "{{PROJECT_SLUG}}": project_slug,
        "{{TARGET}}": target,
        "{{FLEETFORGE_URL}}": fleetforge_url.rstrip("/"),
        "{{FLEETFORGE_REF}}": fleetforge_ref,
    }

    written: list[Path] = []
    dest_dir.mkdir(parents=True, exist_ok=True)
    for rel_path, content in sorted(raw_files.items()):
        out_rel = rel_path.removesuffix(".tmpl")
        out_path = dest_dir / out_rel
        out_path.parent.mkdir(parents=True, exist_ok=True)
        try:
            text = content.decode("utf-8")
            for needle, value in replacements.items():
                text = text.replace(needle, value)
            out_path.write_text(text, encoding="utf-8")
        except UnicodeDecodeError:
            out_path.write_bytes(content)
        written.append(out_path)

    claude_link = dest_dir / "CLAUDE.md"
    if not claude_link.exists() and not claude_link.is_symlink():
        try:
            claude_link.symlink_to("AGENTS.md")
            written.append(claude_link)
        except OSError:
            agents_md = dest_dir / "AGENTS.md"
            if agents_md.is_file():
                claude_link.write_text(agents_md.read_text(encoding="utf-8"), encoding="utf-8")
                written.append(claude_link)

    return written

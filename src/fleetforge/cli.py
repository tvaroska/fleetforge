"""Fleetforge developer & CI/CD CLI (`python -m fleetforge.cli`).

Provides subcommands for authenticating, scaffolding board projects, and running the
Git-driven fast iteration loop (`CI-CD-cli-1` / CUJ-2):

* `login` — authenticate with username/email + password (`POST /v1/auth/login`) or save an
  existing `ffa_...` token, optionally minting a project-scoped CLI token via
  `POST /v1/auth/tokens`, and store credentials in `~/.config/fleetforge/credentials.json`
  (`0600`).
* `whoami` — inspect the active authenticated identity, scopes, and visible projects
  (`GET /v1/auth/me`).
* `logout` — revoke the active token on the server (`POST /v1/auth/logout`) and remove it
  from local credentials.
* `new` — scaffold a new ESP-IDF board firmware repository from the public Fleetforge
  starter template (`templates/esp-idf/`), stamping `AGENTS.md` + `CLAUDE.md`, `justfile`,
  `.github/workflows/fleetforge.yml`, `partitions.csv` (`ab-4m-v1`), `sdkconfig.defaults`,
  and `main/`.
* `push` — upload a compiled `.bin` with Git/CI provenance (`repo_url`, `branch`,
  `commit_sha`, `tag`, `build_url`, `test_passed`) to `POST /v1/artifact`, optionally
  auto-detecting local `git` metadata and/or triggering an explicit project-wide deploy.
* `track` — configure a project's Continuous Deployment tracking rule (`track_mode`,
  `track_ref`, `auto_deploy_target`, `repo_url`) via `PATCH /v1/projects/{slug}`, which
  immediately deploys a matching stored artifact or triggers the project's CI webhook.
* `status` — inspect enrolled boards in a project (`fw_version`, `online`/`power_class`,
  `free_heap`, `rssi`, `secrets_rev`, and active OTA deploy state).
* `logs` — tail structured `up/log` records, `[prev-boot]` crash lines, and heartbeat
  context from a board.
* `secrets` — list, set/rotate, or delete encrypted project secrets (`ff_sec`).
* `dock-sync` — sync an offline USB-C pit dock bundle for a device.
"""

from __future__ import annotations

import argparse
import datetime as dt
import getpass
import http.cookies
import io
import json
import os
import re
import socket
import subprocess
import sys
import tarfile
import urllib.error
import urllib.parse
import urllib.request
from pathlib import Path
from typing import Any

from fleetforge import __version__

DEFAULT_PUBLIC_REPO_URL = "https://github.com/tvaroska/fleetforge"
DEFAULT_FLEETFORGE_URL = "https://bingo.tvaroska.sk"
SUPPORTED_IDF_TARGETS = ("esp32s3", "esp32c6", "esp32", "esp32c3")


def default_credentials_path() -> Path:
    """Return the path to the local CLI credentials file (`0600`)."""
    override = os.environ.get("FLEETFORGE_CREDENTIALS_FILE")
    if override:
        return Path(override).expanduser()
    xdg = os.environ.get("XDG_CONFIG_HOME")
    base = Path(xdg).expanduser() if xdg else (Path.home() / ".config")
    return base / "fleetforge" / "credentials.json"


def load_credentials(path: Path | None = None) -> dict[str, Any]:
    """Load saved CLI credentials from disk, or return an empty dict."""
    target = path or default_credentials_path()
    if not target.is_file():
        return {}
    try:
        data = json.loads(target.read_text(encoding="utf-8"))
        return data if isinstance(data, dict) else {}
    except (OSError, ValueError):
        return {}


def save_credentials(data: dict[str, Any], path: Path | None = None) -> Path:
    """Write CLI credentials to disk with `0700` directory and `0600` file permissions."""
    target = path or default_credentials_path()
    target.parent.mkdir(parents=True, exist_ok=True)
    try:
        os.chmod(target.parent, 0o700)
    except OSError:
        pass
    payload = json.dumps(data, indent=2) + "\n"
    fd = os.open(str(target), os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
    with os.fdopen(fd, "w", encoding="utf-8") as fh:
        fh.write(payload)
    try:
        os.chmod(target, 0o600)
    except OSError:
        pass
    return target


def resolve_url_and_token(
    cli_url: str | None,
    cli_token: str | None,
    *,
    default_url_fallback: str = "http://localhost:8000",
    creds_path: Path | None = None,
) -> tuple[str, str]:
    """Resolve `(base_url, token)` from CLI flags -> environment -> `credentials.json`."""
    creds = load_credentials(creds_path)
    raw_profiles = creds.get("profiles")
    profiles: dict[str, Any] = dict(raw_profiles) if isinstance(raw_profiles, dict) else {}

    raw_url = cli_url or os.environ.get("FLEETFORGE_URL") or creds.get("default_url")
    base_url = str(raw_url or default_url_fallback).rstrip("/")

    raw_token = cli_token or os.environ.get("FLEETFORGE_TOKEN") or ""
    if raw_token:
        return base_url, raw_token

    prof = profiles.get(base_url)
    if isinstance(prof, dict) and isinstance(prof.get("token"), str) and prof["token"]:
        return base_url, prof["token"]

    default_prof_url = creds.get("default_url")
    if isinstance(default_prof_url, str):
        default_prof = profiles.get(default_prof_url.rstrip("/"))
        if (
            isinstance(default_prof, dict)
            and isinstance(default_prof.get("token"), str)
            and default_prof["token"]
        ):
            return base_url, default_prof["token"]

    return base_url, ""


def _extract_session_cookie(set_cookie_headers: list[str]) -> str | None:
    """Extract the `ff_session` `ffa_...` token from `Set-Cookie` response headers."""
    for header in set_cookie_headers:
        cookie: http.cookies.SimpleCookie = http.cookies.SimpleCookie()
        try:
            cookie.load(header)
            if "ff_session" in cookie and cookie["ff_session"].value:
                return cookie["ff_session"].value
        except http.cookies.CookieError:
            pass
        match = re.search(r"(?:^|;\s*|,\s*)ff_session=([^;\s,]+)", header)
        if match:
            return match.group(1)
    return None


def _login_with_password(
    base_url: str,
    username: str,
    password: str,
) -> tuple[int, dict[str, Any], str | None]:
    """Authenticate against `POST /v1/auth/login` and return `(status, body, ff_session_token)`."""
    url = f"{base_url.rstrip('/')}/v1/auth/login"
    body = json.dumps({"username": username, "password": password}).encode("utf-8")
    req = urllib.request.Request(
        url,
        data=body,
        headers={
            "Content-Type": "application/json",
            "Content-Length": str(len(body)),
        },
        method="POST",
    )
    try:
        with urllib.request.urlopen(req, timeout=30) as resp:
            raw = resp.read().decode("utf-8")
            parsed = json.loads(raw) if raw else {}
            set_cookies = resp.headers.get_all("Set-Cookie") or []
            token = _extract_session_cookie(set_cookies) or parsed.get("token")
            return resp.status, parsed, token
    except urllib.error.HTTPError as exc:
        err_body = exc.read().decode("utf-8", errors="replace")
        try:
            parsed = json.loads(err_body)
        except ValueError:
            parsed = {"detail": err_body}
        return exc.code, parsed, None


def _update_dotenv(
    env_path: Path,
    updates: dict[str, str],
) -> None:
    """Update or append `KEY=VALUE` lines in `env_path` (`0600`)."""
    existing_lines: list[str] = []
    if env_path.is_file():
        existing_lines = env_path.read_text(encoding="utf-8").splitlines()

    remaining = dict(updates)
    out_lines: list[str] = []
    for line in existing_lines:
        stripped = line.strip()
        if not stripped or stripped.startswith("#") or "=" not in line:
            out_lines.append(line)
            continue
        key = line.split("=", 1)[0].strip()
        if key in remaining:
            out_lines.append(f"{key}={remaining.pop(key)}")
        else:
            out_lines.append(line)

    for key, val in remaining.items():
        out_lines.append(f"{key}={val}")

    payload = "\n".join(out_lines) + "\n"
    fd = os.open(str(env_path), os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
    with os.fdopen(fd, "w", encoding="utf-8") as fh:
        fh.write(payload)
    try:
        os.chmod(env_path, 0o600)
    except OSError:
        pass


def _git_output(args: list[str], cwd: Path | None = None) -> str | None:
    """Run `git <args>` and return stripped stdout, or `None` if unavailable."""
    try:
        proc = subprocess.run(
            ["git", *args],
            cwd=cwd,
            capture_output=True,
            text=True,
            check=False,
            timeout=5,
        )
    except OSError:
        return None
    if proc.returncode != 0:
        return None
    out = proc.stdout.strip()
    return out if out else None


def detect_git_metadata(cwd: Path | None = None) -> dict[str, str | None]:
    """Detect `repo_url`, `branch`, `commit_sha`, and exact `tag` from a local git checkout."""
    repo_url = _git_output(["config", "--get", "remote.origin.url"], cwd=cwd)
    commit_sha = _git_output(["rev-parse", "HEAD"], cwd=cwd)
    branch = _git_output(["rev-parse", "--abbrev-ref", "HEAD"], cwd=cwd)
    if branch == "HEAD":
        branch = os.environ.get("GITHUB_REF_NAME") or None
    tag = _git_output(["describe", "--tags", "--exact-match"], cwd=cwd)
    return {
        "repo_url": repo_url,
        "branch": branch,
        "commit_sha": commit_sha,
        "tag": tag,
    }


def build_artifact_upload_url(
    base_url: str,
    *,
    target: str,
    version: str,
    partition_layout: str = "ab-4m-v1",
    project: str | None = None,
    repo_url: str | None = None,
    branch: str | None = None,
    commit_sha: str | None = None,
    tag: str | None = None,
    build_url: str | None = None,
    test_passed: bool | None = None,
) -> str:
    """Construct the `POST /v1/artifact` URL with query parameters."""
    params: dict[str, str] = {
        "target": target,
        "version": version,
        "partition_layout": partition_layout,
    }
    if project:
        params["project"] = project
    if repo_url:
        params["repo_url"] = repo_url
    if branch:
        params["branch"] = branch
    if commit_sha:
        params["commit_sha"] = commit_sha
    if tag:
        params["tag"] = tag
    if build_url:
        params["build_url"] = build_url
    if test_passed is not None:
        params["test_passed"] = "true" if test_passed else "false"
    query = urllib.parse.urlencode(params)
    return f"{base_url.rstrip('/')}/v1/artifact?{query}"


def _request_json(
    method: str,
    url: str,
    *,
    token: str,
    body: bytes | None = None,
    content_type: str = "application/octet-stream",
) -> tuple[int, dict[str, Any]]:
    headers = {
        "Authorization": f"Bearer {token}",
        "Content-Type": content_type,
    }
    if body is not None:
        headers["Content-Length"] = str(len(body))
    req = urllib.request.Request(url, data=body, headers=headers, method=method)
    try:
        with urllib.request.urlopen(req, timeout=60) as resp:
            raw = resp.read().decode("utf-8")
            return resp.status, json.loads(raw) if raw else {}
    except urllib.error.HTTPError as exc:
        err_body = exc.read().decode("utf-8", errors="replace")
        try:
            parsed = json.loads(err_body)
        except ValueError:
            parsed = {"detail": err_body}
        return exc.code, parsed


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


def _fetch_remote_template_files(
    repo_url: str, ref: str, template_subdir: str = "templates/esp-idf"
) -> dict[str, bytes]:
    """Download `templates/esp-idf/` from the public GitHub repository tarball."""
    candidates = [ref]
    if ref != "main":
        candidates.append("main")

    last_err: Exception | None = None
    raw_tar: bytes | None = None
    for candidate_ref in candidates:
        url = _github_tarball_url(repo_url, candidate_ref)
        req = urllib.request.Request(url, headers={"User-Agent": f"fleetforge-cli/{__version__}"})
        try:
            with urllib.request.urlopen(req, timeout=30) as resp:
                raw_tar = resp.read()
                break
        except (urllib.error.URLError, OSError) as exc:
            last_err = exc

    if raw_tar is None:
        raise RuntimeError(f"failed to fetch template from {repo_url} ({ref}): {last_err}")

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
        out_path = dest_dir / rel_path
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


def _format_device_table(devices: list[dict[str, Any]]) -> str:
    if not devices:
        return "No enrolled devices found."
    header = (
        f"{'DEVICE_ID':<14} {'NAME':<18} {'TARGET':<8} {'FW':<12} "
        f"{'PRESENCE':<12} {'HEAP':<9} {'RSSI':<6} {'SEC':<4} {'DEPLOY'}"
    )
    lines = [header, "-" * len(header)]
    for d in devices:
        dev_id = str(d.get("device_id") or "")
        name = str(d.get("name") or "-")[:18]
        target = str(d.get("platform_type") or "-")[:8]
        fw = str(d.get("fw_version") or "-")[:12]
        online = bool(d.get("online"))
        power = str(d.get("power_class") or "always_on")
        presence = "online" if online else ("sleepy" if power == "sleepy" else "offline")
        health = d.get("health") or {}
        heap_val = health.get("free_heap")
        heap_str = f"{heap_val // 1024} KB" if isinstance(heap_val, int) else "-"
        rssi_val = health.get("rssi")
        rssi_str = str(rssi_val) if rssi_val is not None else "-"
        sec_rev = str(d.get("secrets_rev") if d.get("secrets_rev") is not None else "-")
        deploy = d.get("deploy")
        if isinstance(deploy, dict) and deploy.get("state"):
            dep_str = f"{deploy.get('state')} ({deploy.get('artifact_version') or '?'})"
            if deploy.get("detail"):
                dep_str += f": {deploy['detail']}"
        else:
            dep_str = "-"
        lines.append(
            f"{dev_id:<14} {name:<18} {target:<8} {fw:<12} "
            f"{presence:<12} {heap_str:<9} {rssi_str:<6} {sec_rev:<4} {dep_str}"
        )
    return "\n".join(lines)


def _build_parser() -> argparse.ArgumentParser:
    common = argparse.ArgumentParser(add_help=False)
    common.add_argument(
        "--url",
        default=argparse.SUPPRESS,
        help="Fleetforge base URL (default: $FLEETFORGE_URL, saved login URL, or http://localhost:8000)",
    )
    common.add_argument(
        "--token",
        default=argparse.SUPPRESS,
        help="Project/admin bearer token ffa_... (default: $FLEETFORGE_TOKEN or saved login)",
    )
    common.add_argument(
        "--credentials-file",
        type=Path,
        default=argparse.SUPPRESS,
        help="Override path to credentials.json (default: ~/.config/fleetforge/credentials.json)",
    )

    parser = argparse.ArgumentParser(
        prog="fleetforge",
        description="Fleetforge CLI for login, board project scaffolding, OTA deploys, logs, and secrets.",
    )
    parser.add_argument(
        "--url",
        default=None,
        help="Fleetforge base URL (default: $FLEETFORGE_URL, saved login URL, or http://localhost:8000)",
    )
    parser.add_argument(
        "--token",
        default=None,
        help="Project/admin bearer token ffa_... (default: $FLEETFORGE_TOKEN or saved login)",
    )
    parser.add_argument(
        "--credentials-file",
        type=Path,
        default=None,
        help="Override path to credentials.json (default: ~/.config/fleetforge/credentials.json)",
    )

    sub = parser.add_subparsers(dest="command", required=True)

    login_p = sub.add_parser(
        "login",
        parents=[common],
        help="Authenticate with username/email + password (or --token) and save local CLI credentials",
    )
    login_p.add_argument(
        "-u",
        "--username",
        default=os.environ.get("FLEETFORGE_USER"),
        help="Username or email (default: $FLEETFORGE_USER or interactive prompt)",
    )
    login_p.add_argument(
        "-p",
        "--password",
        default=os.environ.get("FLEETFORGE_PASSWORD"),
        help="Password (default: $FLEETFORGE_PASSWORD or interactive prompt)",
    )
    login_p.add_argument(
        "--password-stdin",
        action="store_true",
        help="Read password from stdin",
    )
    login_p.add_argument(
        "--project",
        default=None,
        help="Mint a project-pinned CLI token for this project slug after authenticating",
    )
    login_p.add_argument(
        "--scopes",
        default=None,
        help="Comma-separated scopes when minting a token (e.g. read,write:artifacts,deploy)",
    )
    login_p.add_argument(
        "--expires-in-days",
        type=int,
        default=None,
        help="Mint a token expiring in N days via POST /v1/auth/tokens",
    )
    login_p.add_argument(
        "--token-name",
        default=None,
        help="Custom label when minting a token via POST /v1/auth/tokens",
    )
    login_p.add_argument(
        "--write-env",
        nargs="?",
        const=".env",
        default=None,
        metavar="FILE",
        help="Also write FLEETFORGE_URL and FLEETFORGE_TOKEN to .env (or FILE)",
    )

    whoami_p = sub.add_parser(
        "whoami",
        parents=[common],
        help="Show the currently authenticated user, scopes, and visible projects",
    )
    whoami_p.add_argument("--json", action="store_true", help="Output raw JSON")

    sub.add_parser(
        "logout",
        parents=[common],
        help="Revoke the active CLI token on the server and remove it from local credentials",
    )

    new_p = sub.add_parser(
        "new",
        parents=[common],
        help="Scaffold a new ESP-IDF board firmware repository from the Fleetforge template",
    )
    new_p.add_argument("directory", type=Path, help="Target directory to create")
    new_p.add_argument(
        "--name",
        default=None,
        help="Firmware binary/app name (default: derived from directory name)",
    )
    new_p.add_argument(
        "--project",
        default=None,
        help="Fleetforge project slug (default: $FLEETFORGE_PROJECT or directory name)",
    )
    new_p.add_argument(
        "--target",
        choices=SUPPORTED_IDF_TARGETS,
        default="esp32s3",
        help="ESP-IDF chip target (default: esp32s3)",
    )
    new_p.add_argument(
        "--ref",
        default=f"v{__version__}",
        help=f"Fleetforge git tag or branch for idf_component.yml (default: v{__version__})",
    )
    new_p.add_argument(
        "--repo-url",
        default=DEFAULT_PUBLIC_REPO_URL,
        help=f"Fleetforge public git repository URL (default: {DEFAULT_PUBLIC_REPO_URL})",
    )
    new_p.add_argument(
        "--remote",
        action="store_true",
        help="Fetch template tarball from GitHub even if a local checkout is present",
    )
    new_p.add_argument(
        "--force",
        action="store_true",
        help="Allow scaffolding into an existing non-empty directory",
    )
    new_p.add_argument(
        "--create-project",
        action="store_true",
        help="Also create the project on the Fleetforge server (uses saved login or --token)",
    )

    push_p = sub.add_parser("push", parents=[common], help="Upload a firmware .bin with Git provenance")
    push_p.add_argument("binary", type=Path, help="Path to compiled app .bin")
    push_p.add_argument(
        "--project", default=os.environ.get("FLEETFORGE_PROJECT"), help="Project slug"
    )
    push_p.add_argument(
        "--target", required=True, help="Chip target (e.g. esp32c6, esp32s3, esp32)"
    )
    push_p.add_argument(
        "--version", required=True, help="Firmware version label (e.g. 1.2.0-a1b2c3d)"
    )
    push_p.add_argument(
        "--layout",
        default="ab-4m-v1",
        help="Partition layout (default: ab-4m-v1)",
    )
    push_p.add_argument(
        "--repo", default=None, help="Git repository URL (auto-detected if omitted)"
    )
    push_p.add_argument("--branch", default=None, help="Git branch name (auto-detected if omitted)")
    push_p.add_argument("--commit", default=None, help="Git commit SHA (auto-detected if omitted)")
    push_p.add_argument("--tag", default=None, help="Git tag (auto-detected if omitted)")
    push_p.add_argument("--build-url", default=None, help="CI build run URL")
    push_p.add_argument(
        "--no-git",
        action="store_true",
        help="Disable automatic local git provenance detection",
    )
    test_group = push_p.add_mutually_exclusive_group()
    test_group.add_argument(
        "--test-passed",
        dest="test_passed",
        action="store_true",
        default=None,
        help="Mark CI / QEMU verification as passed",
    )
    test_group.add_argument(
        "--test-failed",
        dest="test_passed",
        action="store_false",
        help="Mark CI / QEMU verification as failed (blocks auto-deploy)",
    )
    push_p.add_argument(
        "--deploy",
        choices=["dev", "all"],
        default=None,
        help="Explicitly trigger a project-wide deploy after upload",
    )

    track_p = sub.add_parser("track", parents=[common], help="Configure project continuous deployment tracking")
    track_p.add_argument(
        "--project",
        default=os.environ.get("FLEETFORGE_PROJECT", "default"),
        help="Project slug (default: $FLEETFORGE_PROJECT or 'default')",
    )
    track_p.add_argument(
        "--mode",
        choices=["off", "branch", "tag", "commit"],
        required=True,
        help="Tracking mode",
    )
    track_p.add_argument("--ref", default=None, help="Branch name, tag glob (v*), or commit SHA")
    track_p.add_argument(
        "--auto-deploy",
        choices=["off", "dev", "all"],
        default=None,
        help="Automatic deployment target when a matching build arrives",
    )
    track_p.add_argument("--repo", default=None, help="Git repository URL")

    status_p = sub.add_parser(
        "status",
        aliases=["devices"],
        parents=[common],
        help="Show enrolled boards, health telemetry, and active OTA deploy status",
    )
    status_p.add_argument(
        "--project",
        default=os.environ.get("FLEETFORGE_PROJECT", "default"),
        help="Project slug (default: $FLEETFORGE_PROJECT or 'default')",
    )
    status_p.add_argument("--device", default=None, help="Filter to a single 12-hex device ID")
    status_p.add_argument("--json", action="store_true", help="Output raw JSON")

    logs_p = sub.add_parser(
        "logs",
        parents=[common],
        help="Tail structured up/log lines, [prev-boot] crash records, and heartbeat context",
    )
    logs_p.add_argument(
        "device_id",
        nargs="?",
        default=None,
        help="12-hex device ID (if omitted, uses the first board in --project)",
    )
    logs_p.add_argument(
        "--project",
        default=os.environ.get("FLEETFORGE_PROJECT", "default"),
        help="Project slug (default: $FLEETFORGE_PROJECT or 'default')",
    )
    logs_p.add_argument("--limit", type=int, default=50, help="Max log lines (default: 50)")
    logs_p.add_argument(
        "--level",
        choices=["D", "I", "W", "E"],
        default=None,
        help="Filter by severity level (D, I, W, E)",
    )
    logs_p.add_argument("--json", action="store_true", help="Output raw JSON")

    sec_p = sub.add_parser(
        "secrets",
        parents=[common],
        help="Manage encrypted rotating project secrets (ff_sec)",
    )
    sec_p.add_argument(
        "action",
        choices=["list", "set", "delete"],
        help="Action: list, set <key> <value>, or delete <key>",
    )
    sec_p.add_argument("key", nargs="?", default=None, help="Secret key (<= 15 chars)")
    sec_p.add_argument("value", nargs="?", default=None, help="Secret value (for 'set')")
    sec_p.add_argument(
        "--project",
        default=os.environ.get("FLEETFORGE_PROJECT", "default"),
        help="Project slug (default: $FLEETFORGE_PROJECT or 'default')",
    )

    dock_p = sub.add_parser(
        "dock-sync",
        parents=[common],
        help="Sync an offline USB-C dock bundle (announce, statuses, telemetry, logs) for a device",
    )
    dock_p.add_argument("device_id", help="12-hex device ID (e.g. a4cf12b3de90)")
    dock_p.add_argument(
        "bundle",
        type=Path,
        help="Path to the JSON bundle drained from the USB-C pit dock",
    )

    return parser


def main(argv: list[str] | None = None) -> int:
    parser = _build_parser()
    args = parser.parse_args(argv)
    creds_path: Path | None = args.credentials_file

    default_url_for_cmd = (
        DEFAULT_FLEETFORGE_URL
        if args.command in ("new", "login", "whoami", "logout")
        else "http://localhost:8000"
    )
    resolved_url, resolved_token = resolve_url_and_token(
        args.url,
        args.token,
        default_url_fallback=default_url_for_cmd,
        creds_path=creds_path,
    )
    args.url = resolved_url
    args.token = resolved_token

    if args.command == "login":
        base_url = args.url.rstrip("/")
        explicit_token = args.token if (argv and "--token" in argv) else None
        active_token: str | None = explicit_token

        if not active_token:
            username: str | None = args.username
            if not username:
                if sys.stdin.isatty():
                    username = input("Username or email: ").strip()
                if not username:
                    print(
                        "error: --username / $FLEETFORGE_USER (or --token) is required",
                        file=sys.stderr,
                    )
                    return 2

            password: str | None = None
            if args.password_stdin:
                password = sys.stdin.readline().rstrip("\r\n")
            elif args.password:
                password = args.password
            elif sys.stdin.isatty():
                password = getpass.getpass(f"Password for {username}: ")
            if not password:
                print(
                    "error: password is required (--password, --password-stdin, or $FLEETFORGE_PASSWORD)",
                    file=sys.stderr,
                )
                return 2

            l_status, l_resp, session_token = _login_with_password(base_url, username, password)
            if l_status != 200 or not session_token:
                detail = l_resp.get("detail", l_resp) if isinstance(l_resp, dict) else l_resp
                print(f"login error ({l_status}): {detail}", file=sys.stderr)
                return 1
            active_token = session_token

            # Optionally mint a dedicated or project-pinned token and revoke the temporary session
            wants_mint = any(
                x is not None
                for x in (args.project, args.scopes, args.expires_in_days, args.token_name)
            )
            if wants_mint:
                host = socket.gethostname().split(".")[0] or "local"
                tok_name = args.token_name or (
                    f"cli ({host}:{args.project})" if args.project else f"cli ({host})"
                )
                scopes_list = (
                    [s.strip() for s in args.scopes.split(",") if s.strip()]
                    if args.scopes
                    else (
                        ["read", "write:artifacts", "deploy"]
                        if args.project
                        else ["admin"]
                    )
                )
                mint_payload: dict[str, Any] = {
                    "name": tok_name[:64],
                    "scopes": scopes_list,
                }
                if args.project:
                    mint_payload["project"] = args.project
                if args.expires_in_days is not None:
                    mint_payload["expires_in_days"] = args.expires_in_days
                m_status, m_resp = _request_json(
                    "POST",
                    f"{base_url}/v1/auth/tokens",
                    token=session_token,
                    body=json.dumps(mint_payload).encode("utf-8"),
                    content_type="application/json",
                )
                if m_status != 201 or not isinstance(m_resp.get("token"), str):
                    print(
                        f"token mint error ({m_status}): {m_resp.get('detail', m_resp)}",
                        file=sys.stderr,
                    )
                    return 1
                active_token = m_resp["token"]
                _request_json("POST", f"{base_url}/v1/auth/logout", token=session_token)

        me_status, me_resp = _request_json("GET", f"{base_url}/v1/auth/me", token=active_token)
        if me_status != 200:
            print(
                f"auth verification failed ({me_status}): {me_resp.get('detail', me_resp)}",
                file=sys.stderr,
            )
            return 1

        creds = load_credentials(creds_path)
        raw_profiles = creds.get("profiles")
        profiles: dict[str, Any] = dict(raw_profiles) if isinstance(raw_profiles, dict) else {}
        now_iso = dt.datetime.now(dt.UTC).isoformat()
        profiles[base_url] = {
            "token": active_token,
            "token_id": me_resp.get("token_id"),
            "subject": me_resp.get("subject"),
            "email": me_resp.get("email"),
            "display_name": me_resp.get("display_name"),
            "is_superadmin": bool(me_resp.get("is_superadmin")),
            "scopes": me_resp.get("scopes", []),
            "project": args.project,
            "expires_at": me_resp.get("expires_at"),
            "saved_at": now_iso,
        }
        creds["default_url"] = base_url
        creds["profiles"] = profiles
        saved_file = save_credentials(creds, creds_path)

        if args.write_env:
            env_updates = {
                "FLEETFORGE_URL": base_url,
                "FLEETFORGE_TOKEN": active_token,
            }
            if args.project:
                env_updates["FLEETFORGE_PROJECT"] = args.project
            _update_dotenv(Path(args.write_env), env_updates)

        print(
            json.dumps(
                {
                    "status": "authenticated",
                    "url": base_url,
                    "subject": me_resp.get("subject"),
                    "email": me_resp.get("email"),
                    "display_name": me_resp.get("display_name"),
                    "is_superadmin": bool(me_resp.get("is_superadmin")),
                    "scopes": me_resp.get("scopes", []),
                    "expires_at": me_resp.get("expires_at"),
                    "projects": [
                        {"slug": p.get("slug"), "role": p.get("role")}
                        for p in (me_resp.get("projects") or [])
                        if isinstance(p, dict)
                    ],
                    "credentials_file": str(saved_file),
                    "env_file": str(args.write_env) if args.write_env else None,
                },
                indent=2,
            )
        )
        return 0

    if args.command == "whoami":
        if not args.token:
            print(
                "error: not authenticated — run 'fleetforge login' or pass --token / $FLEETFORGE_TOKEN",
                file=sys.stderr,
            )
            return 2
        status_code, me_resp = _request_json(
            "GET", f"{args.url.rstrip('/')}/v1/auth/me", token=args.token
        )
        if status_code != 200:
            print(f"error ({status_code}): {me_resp.get('detail', me_resp)}", file=sys.stderr)
            return 1
        if args.json:
            print(json.dumps(me_resp, indent=2))
            return 0
        projs = ", ".join(
            f"{p.get('slug')} ({p.get('role')})"
            for p in (me_resp.get("projects") or [])
            if isinstance(p, dict)
        )
        print(f"URL:        {args.url}")
        print(f"User:       {me_resp.get('subject')} ({me_resp.get('email') or 'no email'})")
        if me_resp.get("display_name"):
            print(f"Name:       {me_resp.get('display_name')}")
        print(f"Superadmin: {'yes' if me_resp.get('is_superadmin') else 'no'}")
        print(f"Scopes:     {', '.join(me_resp.get('scopes') or [])}")
        print(f"Expires:    {me_resp.get('expires_at') or 'never'}")
        print(f"Projects:   {projs or '-'}")
        return 0

    if args.command == "logout":
        base_url = args.url.rstrip("/")
        if args.token:
            _request_json("POST", f"{base_url}/v1/auth/logout", token=args.token)
        creds = load_credentials(creds_path)
        raw_saved = creds.get("profiles")
        saved_profiles: dict[str, Any] = dict(raw_saved) if isinstance(raw_saved, dict) else {}
        removed = saved_profiles.pop(base_url, None) is not None
        if creds.get("default_url") == base_url:
            creds["default_url"] = next(iter(saved_profiles.keys()), None)
        creds["profiles"] = saved_profiles
        if creds_path or default_credentials_path().is_file():
            save_credentials(creds, creds_path)
        print(
            json.dumps(
                {"status": "logged_out", "url": base_url, "removed_saved_profile": removed},
                indent=2,
            )
        )
        return 0

    if args.command == "new":
        dest_dir: Path = args.directory
        raw_base = dest_dir.resolve().name if dest_dir.name in (".", "") else dest_dir.name
        app_name = _sanitize_app_name(args.name or raw_base)
        project_slug = _sanitize_project_slug(
            args.project or os.environ.get("FLEETFORGE_PROJECT") or raw_base
        )
        effective_url = args.url or DEFAULT_FLEETFORGE_URL
        try:
            written = render_template_project(
                dest_dir,
                app_name=app_name,
                project_slug=project_slug,
                target=args.target,
                fleetforge_url=effective_url,
                fleetforge_ref=args.ref,
                repo_url=args.repo_url,
                prefer_remote=args.remote,
                force=args.force,
            )
        except (FileExistsError, RuntimeError, OSError) as exc:
            print(f"error: {exc}", file=sys.stderr)
            return 1

        if args.create_project:
            if not args.token:
                print(
                    "error: --create-project requires 'fleetforge login' or --token / $FLEETFORGE_TOKEN",
                    file=sys.stderr,
                )
                return 2
            create_url = f"{effective_url.rstrip('/')}/v1/projects"
            body = json.dumps({"slug": project_slug, "name": project_slug}).encode("utf-8")
            p_status, p_resp = _request_json(
                "POST",
                create_url,
                token=args.token,
                body=body,
                content_type="application/json",
            )
            if p_status not in (200, 201, 409):
                print(
                    f"project create error ({p_status}): {p_resp.get('detail', p_resp)}",
                    file=sys.stderr,
                )
                return 1

        print(
            json.dumps(
                {
                    "directory": str(dest_dir),
                    "app_name": app_name,
                    "project": project_slug,
                    "target": args.target,
                    "fleetforge_ref": args.ref,
                    "fleetforge_url": effective_url,
                    "files": [p.relative_to(dest_dir).as_posix() for p in written],
                },
                indent=2,
            )
        )
        return 0

    if not args.token:
        print(
            "error: not authenticated — run 'fleetforge login' or pass --token / $FLEETFORGE_TOKEN",
            file=sys.stderr,
        )
        return 2

    if args.command == "push":
        bin_path: Path = args.binary
        if not bin_path.is_file():
            print(f"error: binary not found: {bin_path}", file=sys.stderr)
            return 2
        data = bin_path.read_bytes()
        git_meta = (
            {"repo_url": None, "branch": None, "commit_sha": None, "tag": None}
            if args.no_git
            else detect_git_metadata(bin_path.parent)
        )
        url = build_artifact_upload_url(
            args.url,
            target=args.target,
            version=args.version,
            partition_layout=args.layout,
            project=args.project,
            repo_url=args.repo if args.repo is not None else git_meta["repo_url"],
            branch=args.branch if args.branch is not None else git_meta["branch"],
            commit_sha=args.commit if args.commit is not None else git_meta["commit_sha"],
            tag=args.tag if args.tag is not None else git_meta["tag"],
            build_url=args.build_url,
            test_passed=args.test_passed,
        )
        status_code, resp = _request_json("POST", url, token=args.token, body=data)
        if status_code not in (200, 201):
            print(
                f"error ({status_code}): {resp.get('detail', resp)}",
                file=sys.stderr,
            )
            return 1
        print(json.dumps(resp, indent=2))

        if args.deploy:
            proj_slug = args.project or "default"
            deploy_url = f"{args.url.rstrip('/')}/v1/projects/{proj_slug}/deploy"
            deploy_body = json.dumps(
                {
                    "version": args.version,
                    "target_filter": args.deploy,
                    "apply": "auto",
                }
            ).encode("utf-8")
            d_status, d_resp = _request_json(
                "POST",
                deploy_url,
                token=args.token,
                body=deploy_body,
                content_type="application/json",
            )
            if d_status != 202:
                print(
                    f"deploy error ({d_status}): {d_resp.get('detail', d_resp)}",
                    file=sys.stderr,
                )
                return 1
            print(json.dumps(d_resp, indent=2))
        return 0

    if args.command == "track":
        patch_url = f"{args.url.rstrip('/')}/v1/projects/{args.project}"
        payload: dict[str, Any] = {"track_mode": args.mode}
        if args.ref is not None:
            payload["track_ref"] = args.ref
        if args.auto_deploy is not None:
            payload["auto_deploy_target"] = args.auto_deploy
        if args.repo is not None:
            payload["repo_url"] = args.repo
        status_code, resp = _request_json(
            "PATCH",
            patch_url,
            token=args.token,
            body=json.dumps(payload).encode("utf-8"),
            content_type="application/json",
        )
        if status_code != 200:
            print(f"error ({status_code}): {resp.get('detail', resp)}", file=sys.stderr)
            return 1
        print(json.dumps(resp, indent=2))
        return 0

    if args.command in ("status", "devices"):
        query = urllib.parse.urlencode({"project": args.project})
        url = f"{args.url.rstrip('/')}/v1/devices?{query}"
        status_code, resp = _request_json("GET", url, token=args.token)
        if status_code != 200:
            print(f"error ({status_code}): {resp.get('detail', resp)}", file=sys.stderr)
            return 1
        devices: list[dict[str, Any]] = list(resp.get("devices") or [])
        if args.device:
            devices = [d for d in devices if d.get("device_id") == args.device]
        if args.json:
            print(json.dumps({"devices": devices}, indent=2))
        else:
            print(_format_device_table(devices))
        return 0

    if args.command == "logs":
        device_id: str | None = args.device_id
        if not device_id:
            query = urllib.parse.urlencode({"project": args.project})
            dev_url = f"{args.url.rstrip('/')}/v1/devices?{query}"
            d_status, d_resp = _request_json("GET", dev_url, token=args.token)
            if d_status != 200:
                print(
                    f"error ({d_status}): {d_resp.get('detail', d_resp)}",
                    file=sys.stderr,
                )
                return 1
            devs = list(d_resp.get("devices") or [])
            if not devs:
                print(
                    f"error: no devices enrolled in project '{args.project}'",
                    file=sys.stderr,
                )
                return 1
            device_id = str(devs[0]["device_id"])

        l_query = urllib.parse.urlencode({"limit": str(args.limit)})
        logs_url = f"{args.url.rstrip('/')}/v1/devices/{device_id}/logs?{l_query}"
        status_code, resp = _request_json("GET", logs_url, token=args.token)
        if status_code != 200:
            print(f"error ({status_code}): {resp.get('detail', resp)}", file=sys.stderr)
            return 1
        lines: list[dict[str, Any]] = list(resp.get("lines") or [])
        if args.level:
            lines = [ln for ln in lines if ln.get("level") == args.level]
        if args.json:
            print(json.dumps({"device_id": device_id, "lines": lines}, indent=2))
            return 0
        for ln in reversed(lines):
            ts = str(ln.get("logged_at") or ln.get("received_at") or "")
            lvl = str(ln.get("level") or "I")
            txt = str(ln.get("text") or "")
            ctx = ln.get("context") or {}
            ctx_parts: list[str] = []
            if isinstance(ctx, dict):
                if "firmware.version" in ctx:
                    ctx_parts.append(f"fw={ctx['firmware.version']}")
                if "device.free_heap_bytes" in ctx:
                    ctx_parts.append(f"heap={ctx['device.free_heap_bytes']}")
                if "device.rssi_dbm" in ctx:
                    ctx_parts.append(f"rssi={ctx['device.rssi_dbm']}")
            suffix = f" ({' '.join(ctx_parts)})" if ctx_parts else ""
            print(f"{ts} [{lvl}] {txt}{suffix}")
        return 0

    if args.command == "secrets":
        base_sec_url = f"{args.url.rstrip('/')}/v1/projects/{args.project}/secrets"
        if args.action == "list":
            status_code, resp = _request_json("GET", base_sec_url, token=args.token)
        elif args.action == "set":
            if not args.key or args.value is None:
                print("error: 'secrets set' requires <key> and <value>", file=sys.stderr)
                return 2
            status_code, resp = _request_json(
                "PUT",
                f"{base_sec_url}/{urllib.parse.quote(args.key)}",
                token=args.token,
                body=json.dumps({"value": args.value}).encode("utf-8"),
                content_type="application/json",
            )
        else:
            if not args.key:
                print("error: 'secrets delete' requires <key>", file=sys.stderr)
                return 2
            status_code, resp = _request_json(
                "DELETE",
                f"{base_sec_url}/{urllib.parse.quote(args.key)}",
                token=args.token,
            )
        if status_code != 200:
            print(f"error ({status_code}): {resp.get('detail', resp)}", file=sys.stderr)
            return 1
        print(json.dumps(resp, indent=2))
        return 0

    if args.command == "dock-sync":
        bundle_path: Path = args.bundle
        if not bundle_path.is_file():
            print(f"error: bundle not found: {bundle_path}", file=sys.stderr)
            return 2
        try:
            raw_bundle = json.loads(bundle_path.read_text(encoding="utf-8"))
        except (OSError, ValueError) as exc:
            print(f"error: invalid JSON bundle {bundle_path}: {exc}", file=sys.stderr)
            return 2
        sync_url = f"{args.url.rstrip('/')}/v1/devices/{args.device_id}/dock-sync"
        status_code, resp = _request_json(
            "POST",
            sync_url,
            token=args.token,
            body=json.dumps(raw_bundle).encode("utf-8"),
            content_type="application/json",
        )
        if status_code != 200:
            print(f"error ({status_code}): {resp.get('detail', resp)}", file=sys.stderr)
            return 1
        print(json.dumps(resp, indent=2))
        return 0

    return 2


if __name__ == "__main__":
    raise SystemExit(main())

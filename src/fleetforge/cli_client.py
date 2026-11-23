"""CLI credentials, Git provenance, and HTTP transport helpers."""

from __future__ import annotations

import http.cookies
import json
import os
import re
import subprocess
import urllib.error
import urllib.parse
import urllib.request
from pathlib import Path
from typing import Any, Protocol

DEFAULT_PUBLIC_REPO_URL = "https://github.com/tvaroska/fleetforge"
DEFAULT_FLEETFORGE_URL = "https://bingo.tvaroska.sk"
SUPPORTED_IDF_TARGETS = ("esp32s3", "esp32c6", "esp32", "esp32c3")


class RequestJsonFn(Protocol):
    def __call__(
        self,
        method: str,
        url: str,
        *,
        token: str,
        body: bytes | None = None,
        content_type: str = "application/octet-stream",
    ) -> tuple[int, dict[str, Any]]: ...


class LoginWithPasswordFn(Protocol):
    def __call__(
        self,
        base_url: str,
        username: str,
        password: str,
    ) -> tuple[int, dict[str, Any], str | None]: ...


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


def _saved_profile_str(creds: dict[str, Any], base_url: str, key: str) -> str | None:
    raw_profiles = creds.get("profiles")
    if not isinstance(raw_profiles, dict):
        return None
    candidates = [base_url.rstrip("/")]
    default_url = creds.get("default_url")
    if isinstance(default_url, str):
        candidates.append(default_url.rstrip("/"))
    for url_key in candidates:
        prof = raw_profiles.get(url_key)
        if isinstance(prof, dict):
            val = prof.get(key)
            if isinstance(val, str) and val:
                return val
    return None


def resolve_project(
    explicit_project: str | None,
    *,
    base_url: str,
    token: str | None = None,
    creds_path: Path | None = None,
    query_server: bool = False,
    request_json: RequestJsonFn | None = None,
) -> str | None:
    """Resolve project slug from flag/env -> saved CLI profile -> `/v1/auth/me`."""
    if explicit_project:
        return explicit_project
    if env_proj := os.environ.get("FLEETFORGE_PROJECT"):
        return env_proj
    if saved := _saved_profile_str(load_credentials(creds_path), base_url, "project"):
        return saved
    if not (query_server and token):
        return None
    requester = request_json or _request_json
    me_status, me_resp = requester("GET", f"{base_url.rstrip('/')}/v1/auth/me", token=token)
    if me_status != 200:
        return None
    projs = me_resp.get("projects")
    if isinstance(projs, list) and len(projs) == 1 and isinstance(projs[0], dict):
        slug = projs[0].get("slug")
        return slug if isinstance(slug, str) and slug else None
    return None


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
    optional = {
        "project": project,
        "repo_url": repo_url,
        "branch": branch,
        "commit_sha": commit_sha,
        "tag": tag,
        "build_url": build_url,
    }
    for key, val in optional.items():
        if val:
            params[key] = val
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

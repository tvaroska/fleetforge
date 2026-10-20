"""Fleetforge developer & CI/CD CLI (`python -m fleetforge.cli`).

Provides two subcommands for the Git-driven fast iteration loop (`CI-CD-cli-1` / CUJ-2):

* `push` — upload a compiled `.bin` with Git/CI provenance (`repo_url`, `branch`,
  `commit_sha`, `tag`, `build_url`, `test_passed`) to `POST /v1/artifact`, optionally
  auto-detecting local `git` metadata and/or triggering an explicit project-wide deploy.
* `track` — configure a project's Continuous Deployment tracking rule (`track_mode`,
  `track_ref`, `auto_deploy_target`, `repo_url`) via `PATCH /v1/projects/{slug}`, which
  immediately deploys a matching stored artifact or triggers the project's CI webhook.
"""

from __future__ import annotations

import argparse
import json
import os
import subprocess
import sys
import urllib.error
import urllib.parse
import urllib.request
from pathlib import Path
from typing import Any


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


def _build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="fleetforge",
        description="Fleetforge CLI for Git-driven firmware upload and continuous deployment.",
    )
    parser.add_argument(
        "--url",
        default=os.environ.get("FLEETFORGE_URL", "http://localhost:8000"),
        help="Fleetforge base URL (default: $FLEETFORGE_URL or http://localhost:8000)",
    )
    parser.add_argument(
        "--token",
        default=os.environ.get("FLEETFORGE_TOKEN", ""),
        help="Project/admin bearer token ffa_... (default: $FLEETFORGE_TOKEN)",
    )

    sub = parser.add_subparsers(dest="command", required=True)

    push_p = sub.add_parser("push", help="Upload a firmware .bin with Git provenance")
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

    track_p = sub.add_parser("track", help="Configure project continuous deployment tracking")
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

    return parser


def main(argv: list[str] | None = None) -> int:
    parser = _build_parser()
    args = parser.parse_args(argv)

    if not args.token:
        print("error: --token or $FLEETFORGE_TOKEN is required", file=sys.stderr)
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

    return 2


if __name__ == "__main__":
    raise SystemExit(main())

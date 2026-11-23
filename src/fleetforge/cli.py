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
import os
import sys
from pathlib import Path
from typing import Any

from fleetforge import __version__
from fleetforge.cli_client import (
    DEFAULT_FLEETFORGE_URL,
    DEFAULT_PUBLIC_REPO_URL,
    SUPPORTED_IDF_TARGETS,
    _extract_session_cookie,
    _git_output,
    _login_with_password,
    _request_json,
    _update_dotenv,
    build_artifact_upload_url,
    default_credentials_path,
    detect_git_metadata,
    load_credentials,
    resolve_url_and_token,
    save_credentials,
)
from fleetforge.cli_client import (
    resolve_project as _resolve_project_impl,
)
from fleetforge.cli_commands import (
    COMMAND_HANDLERS,
    UNAUTHENTICATED_COMMANDS,
    CliContext,
    _format_device_table,
)
from fleetforge.cli_templates import (
    _fetch_remote_template_files,
    _github_tarball_url,
    _sanitize_app_name,
    _sanitize_project_slug,
    load_template_files,
    render_template_project,
)

__all__ = [
    "DEFAULT_FLEETFORGE_URL",
    "DEFAULT_PUBLIC_REPO_URL",
    "SUPPORTED_IDF_TARGETS",
    "_build_parser",
    "_extract_session_cookie",
    "_fetch_remote_template_files",
    "_format_device_table",
    "_git_output",
    "_github_tarball_url",
    "_login_with_password",
    "_request_json",
    "_sanitize_app_name",
    "_sanitize_project_slug",
    "_update_dotenv",
    "build_artifact_upload_url",
    "default_credentials_path",
    "detect_git_metadata",
    "load_credentials",
    "load_template_files",
    "main",
    "render_template_project",
    "resolve_project",
    "resolve_url_and_token",
    "save_credentials",
]


def resolve_project(
    explicit_project: str | None,
    *,
    base_url: str,
    token: str | None = None,
    creds_path: Path | None = None,
    query_server: bool = False,
) -> str | None:
    """Resolve project slug from flag/env -> saved CLI profile -> `/v1/auth/me`."""
    return _resolve_project_impl(
        explicit_project,
        base_url=base_url,
        token=token,
        creds_path=creds_path,
        query_server=query_server,
        request_json=_request_json,
    )


def _add_auth_subparsers(
    sub: argparse._SubParsersAction[Any], common: argparse.ArgumentParser
) -> None:
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


def _add_project_subparsers(
    sub: argparse._SubParsersAction[Any], common: argparse.ArgumentParser
) -> None:
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

    push_p = sub.add_parser(
        "push", parents=[common], help="Upload a firmware .bin with Git provenance"
    )
    push_p.add_argument("binary", type=Path, help="Path to compiled app .bin")
    push_p.add_argument(
        "--project",
        default=None,
        help="Project slug (default: $FLEETFORGE_PROJECT, saved login project, or 'default')",
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

    track_p = sub.add_parser(
        "track", parents=[common], help="Configure project continuous deployment tracking"
    )
    track_p.add_argument(
        "--project",
        default=None,
        help="Project slug (default: $FLEETFORGE_PROJECT, saved login project, or 'default')",
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


def _add_device_subparsers(
    sub: argparse._SubParsersAction[Any], common: argparse.ArgumentParser
) -> None:
    status_p = sub.add_parser(
        "status",
        aliases=["devices"],
        parents=[common],
        help="Show enrolled boards, health telemetry, and active OTA deploy status",
    )
    status_p.add_argument(
        "--project",
        default=None,
        help="Project slug (default: $FLEETFORGE_PROJECT, saved login project, or 'default')",
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
        default=None,
        help="Project slug (default: $FLEETFORGE_PROJECT, saved login project, or 'default')",
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
        default=None,
        help="Project slug (default: $FLEETFORGE_PROJECT, saved login project, or 'default')",
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
    _add_auth_subparsers(sub, common)
    _add_project_subparsers(sub, common)
    _add_device_subparsers(sub, common)
    return parser


def main(argv: list[str] | None = None) -> int:
    parser = _build_parser()
    args = parser.parse_args(argv)
    creds_path: Path | None = args.credentials_file

    default_url_for_cmd = (
        DEFAULT_FLEETFORGE_URL
        if args.command in UNAUTHENTICATED_COMMANDS
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

    if args.command not in UNAUTHENTICATED_COMMANDS and not args.token:
        print(
            "error: not authenticated — run 'fleetforge login' or pass --token / $FLEETFORGE_TOKEN",
            file=sys.stderr,
        )
        return 2

    handler = COMMAND_HANDLERS.get(args.command)
    if handler is None:
        return 2

    # Look up transport helpers from this module's globals so tests patching
    # `fleetforge.cli._request_json` or `fleetforge.cli._login_with_password` work seamlessly.
    ctx = CliContext(
        creds_path=creds_path,
        argv=argv,
        request_json=_request_json,
        login_with_password=_login_with_password,
    )
    return handler(args, ctx)


if __name__ == "__main__":
    raise SystemExit(main())

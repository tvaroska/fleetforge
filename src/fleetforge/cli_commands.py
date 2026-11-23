"""Per-subcommand handlers and dispatch table for the Fleetforge CLI."""

from __future__ import annotations

import argparse
import datetime as dt
import getpass
import json
import os
import socket
import sys
import urllib.parse
from collections.abc import Callable
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from fleetforge.cli_client import (
    DEFAULT_FLEETFORGE_URL,
    LoginWithPasswordFn,
    RequestJsonFn,
    _update_dotenv,
    build_artifact_upload_url,
    default_credentials_path,
    detect_git_metadata,
    load_credentials,
    resolve_project,
    save_credentials,
)
from fleetforge.cli_templates import (
    _sanitize_app_name,
    _sanitize_project_slug,
    render_template_project,
)


@dataclass(frozen=True, slots=True)
class CliContext:
    """Resolved runtime context passed to every subcommand handler."""

    creds_path: Path | None
    argv: list[str] | None
    request_json: RequestJsonFn
    login_with_password: LoginWithPasswordFn


CommandHandler = Callable[[argparse.Namespace, CliContext], int]
UNAUTHENTICATED_COMMANDS = frozenset({"login", "whoami", "logout", "new"})


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


def _prompt_credentials(args: argparse.Namespace) -> tuple[str | None, str | None]:
    username: str | None = args.username
    if not username:
        if sys.stdin.isatty():
            username = input("Username or email: ").strip()
        if not username:
            print(
                "error: --username / $FLEETFORGE_USER (or --token) is required",
                file=sys.stderr,
            )
            return None, None

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
        return None, None
    return username, password


def _mint_cli_token(
    base_url: str,
    session_token: str,
    args: argparse.Namespace,
    ctx: CliContext,
) -> str | None:
    host = socket.gethostname().split(".")[0] or "local"
    tok_name = args.token_name or (
        f"cli ({host}:{args.project})" if args.project else f"cli ({host})"
    )
    scopes_list = (
        [s.strip() for s in args.scopes.split(",") if s.strip()]
        if args.scopes
        else (["read", "write:artifacts", "deploy"] if args.project else ["admin"])
    )
    mint_payload: dict[str, Any] = {
        "name": tok_name[:64],
        "scopes": scopes_list,
    }
    if args.project:
        mint_payload["project"] = args.project
    if args.expires_in_days is not None:
        mint_payload["expires_in_days"] = args.expires_in_days
    m_status, m_resp = ctx.request_json(
        "POST",
        f"{base_url}/v1/auth/tokens",
        token=session_token,
        body=json.dumps(mint_payload).encode("utf-8"),
        content_type="application/json",
    )
    minted = m_resp.get("token")
    if m_status != 201 or not isinstance(minted, str):
        print(
            f"token mint error ({m_status}): {m_resp.get('detail', m_resp)}",
            file=sys.stderr,
        )
        return None
    ctx.request_json("POST", f"{base_url}/v1/auth/logout", token=session_token)
    return minted


def cmd_login(args: argparse.Namespace, ctx: CliContext) -> int:
    base_url = args.url.rstrip("/")
    has_token_flag = bool(
        ctx.argv and any(arg == "--token" or arg.startswith("--token=") for arg in ctx.argv)
    )
    active_token: str | None = args.token if has_token_flag else None

    if not active_token:
        username, password = _prompt_credentials(args)
        if not username or not password:
            return 2

        l_status, l_resp, session_token = ctx.login_with_password(base_url, username, password)
        if l_status != 200 or not session_token:
            detail = l_resp.get("detail", l_resp) if isinstance(l_resp, dict) else l_resp
            print(f"login error ({l_status}): {detail}", file=sys.stderr)
            return 1
        active_token = session_token

        wants_mint = any(
            x is not None
            for x in (args.project, args.scopes, args.expires_in_days, args.token_name)
        )
        if wants_mint:
            active_token = _mint_cli_token(base_url, session_token, args, ctx)
            if not active_token:
                return 1

    me_status, me_resp = ctx.request_json("GET", f"{base_url}/v1/auth/me", token=active_token)
    if me_status != 200:
        print(
            f"auth verification failed ({me_status}): {me_resp.get('detail', me_resp)}",
            file=sys.stderr,
        )
        return 1

    creds = load_credentials(ctx.creds_path)
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
    saved_file = save_credentials(creds, ctx.creds_path)

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


def cmd_whoami(args: argparse.Namespace, ctx: CliContext) -> int:
    if not args.token:
        print(
            "error: not authenticated — run 'fleetforge login' or pass --token / $FLEETFORGE_TOKEN",
            file=sys.stderr,
        )
        return 2
    status_code, me_resp = ctx.request_json(
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


def cmd_logout(args: argparse.Namespace, ctx: CliContext) -> int:
    base_url = args.url.rstrip("/")
    if args.token:
        ctx.request_json("POST", f"{base_url}/v1/auth/logout", token=args.token)
    creds = load_credentials(ctx.creds_path)
    raw_saved = creds.get("profiles")
    saved_profiles: dict[str, Any] = dict(raw_saved) if isinstance(raw_saved, dict) else {}
    removed = saved_profiles.pop(base_url, None) is not None
    if creds.get("default_url") == base_url:
        creds["default_url"] = next(iter(saved_profiles.keys()), None)
    creds["profiles"] = saved_profiles
    if ctx.creds_path or default_credentials_path().is_file():
        save_credentials(creds, ctx.creds_path)
    print(
        json.dumps(
            {"status": "logged_out", "url": base_url, "removed_saved_profile": removed},
            indent=2,
        )
    )
    return 0


def cmd_new(args: argparse.Namespace, ctx: CliContext) -> int:
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
        p_status, p_resp = ctx.request_json(
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


def cmd_push(args: argparse.Namespace, ctx: CliContext) -> int:
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
    resolved_proj = resolve_project(
        args.project,
        base_url=args.url,
        creds_path=ctx.creds_path,
        request_json=ctx.request_json,
    )
    url = build_artifact_upload_url(
        args.url,
        target=args.target,
        version=args.version,
        partition_layout=args.layout,
        project=resolved_proj,
        repo_url=args.repo if args.repo is not None else git_meta["repo_url"],
        branch=args.branch if args.branch is not None else git_meta["branch"],
        commit_sha=args.commit if args.commit is not None else git_meta["commit_sha"],
        tag=args.tag if args.tag is not None else git_meta["tag"],
        build_url=args.build_url,
        test_passed=args.test_passed,
    )
    status_code, resp = ctx.request_json("POST", url, token=args.token, body=data)
    if status_code not in (200, 201):
        print(f"error ({status_code}): {resp.get('detail', resp)}", file=sys.stderr)
        return 1
    print(json.dumps(resp, indent=2))

    if args.deploy:
        proj_slug = (
            resolve_project(
                args.project,
                base_url=args.url,
                token=args.token,
                creds_path=ctx.creds_path,
                query_server=True,
                request_json=ctx.request_json,
            )
            or "default"
        )
        deploy_url = f"{args.url.rstrip('/')}/v1/projects/{proj_slug}/deploy"
        deploy_body = json.dumps(
            {
                "version": args.version,
                "target_filter": args.deploy,
                "apply": "auto",
            }
        ).encode("utf-8")
        d_status, d_resp = ctx.request_json(
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


def cmd_track(args: argparse.Namespace, ctx: CliContext) -> int:
    proj_slug = (
        resolve_project(
            args.project,
            base_url=args.url,
            token=args.token,
            creds_path=ctx.creds_path,
            query_server=True,
            request_json=ctx.request_json,
        )
        or "default"
    )
    patch_url = f"{args.url.rstrip('/')}/v1/projects/{proj_slug}"
    payload: dict[str, Any] = {"track_mode": args.mode}
    if args.ref is not None:
        payload["track_ref"] = args.ref
    if args.auto_deploy is not None:
        payload["auto_deploy_target"] = args.auto_deploy
    if args.repo is not None:
        payload["repo_url"] = args.repo
    status_code, resp = ctx.request_json(
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


def cmd_status(args: argparse.Namespace, ctx: CliContext) -> int:
    opt_proj = resolve_project(
        args.project,
        base_url=args.url,
        creds_path=ctx.creds_path,
        request_json=ctx.request_json,
    )
    url = f"{args.url.rstrip('/')}/v1/devices"
    if opt_proj:
        url = f"{url}?{urllib.parse.urlencode({'project': opt_proj})}"
    status_code, resp = ctx.request_json("GET", url, token=args.token)
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


def _resolve_logs_device_id(args: argparse.Namespace, ctx: CliContext) -> str | None:
    if args.device_id:
        return str(args.device_id)
    opt_proj = resolve_project(
        args.project,
        base_url=args.url,
        creds_path=ctx.creds_path,
        request_json=ctx.request_json,
    )
    dev_url = f"{args.url.rstrip('/')}/v1/devices"
    if opt_proj:
        dev_url = f"{dev_url}?{urllib.parse.urlencode({'project': opt_proj})}"
    d_status, d_resp = ctx.request_json("GET", dev_url, token=args.token)
    if d_status != 200:
        print(f"error ({d_status}): {d_resp.get('detail', d_resp)}", file=sys.stderr)
        return None
    devs = list(d_resp.get("devices") or [])
    if not devs:
        print(
            f"error: no devices enrolled in project '{opt_proj or 'default'}'",
            file=sys.stderr,
        )
        return None
    return str(devs[0]["device_id"])


def _format_log_line(ln: dict[str, Any]) -> str:
    ts = str(ln.get("logged_at") or ln.get("received_at") or "")
    lvl = str(ln.get("level") or "I")
    txt = str(ln.get("text") or "")
    ctx_map = ln.get("context") or {}
    ctx_parts: list[str] = []
    if isinstance(ctx_map, dict):
        if "firmware.version" in ctx_map:
            ctx_parts.append(f"fw={ctx_map['firmware.version']}")
        if "device.free_heap_bytes" in ctx_map:
            ctx_parts.append(f"heap={ctx_map['device.free_heap_bytes']}")
        if "device.rssi_dbm" in ctx_map:
            ctx_parts.append(f"rssi={ctx_map['device.rssi_dbm']}")
    suffix = f" ({' '.join(ctx_parts)})" if ctx_parts else ""
    return f"{ts} [{lvl}] {txt}{suffix}"


def cmd_logs(args: argparse.Namespace, ctx: CliContext) -> int:
    device_id = _resolve_logs_device_id(args, ctx)
    if not device_id:
        return 1

    l_query = urllib.parse.urlencode({"limit": str(args.limit)})
    logs_url = f"{args.url.rstrip('/')}/v1/devices/{device_id}/logs?{l_query}"
    status_code, resp = ctx.request_json("GET", logs_url, token=args.token)
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
        print(_format_log_line(ln))
    return 0


def cmd_secrets(args: argparse.Namespace, ctx: CliContext) -> int:
    proj_slug = (
        resolve_project(
            args.project,
            base_url=args.url,
            token=args.token,
            creds_path=ctx.creds_path,
            query_server=True,
            request_json=ctx.request_json,
        )
        or "default"
    )
    base_sec_url = f"{args.url.rstrip('/')}/v1/projects/{proj_slug}/secrets"
    if args.action == "list":
        status_code, resp = ctx.request_json("GET", base_sec_url, token=args.token)
    elif args.action == "set":
        if not args.key or args.value is None:
            print("error: 'secrets set' requires <key> and <value>", file=sys.stderr)
            return 2
        status_code, resp = ctx.request_json(
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
        status_code, resp = ctx.request_json(
            "DELETE",
            f"{base_sec_url}/{urllib.parse.quote(args.key)}",
            token=args.token,
        )
    if status_code != 200:
        print(f"error ({status_code}): {resp.get('detail', resp)}", file=sys.stderr)
        return 1
    print(json.dumps(resp, indent=2))
    return 0


def cmd_dock_sync(args: argparse.Namespace, ctx: CliContext) -> int:
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
    status_code, resp = ctx.request_json(
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


COMMAND_HANDLERS: dict[str, CommandHandler] = {
    "login": cmd_login,
    "whoami": cmd_whoami,
    "logout": cmd_logout,
    "new": cmd_new,
    "push": cmd_push,
    "track": cmd_track,
    "status": cmd_status,
    "devices": cmd_status,
    "logs": cmd_logs,
    "secrets": cmd_secrets,
    "dock-sync": cmd_dock_sync,
}

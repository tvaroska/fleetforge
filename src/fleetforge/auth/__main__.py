"""`python -m fleetforge.auth hash-password` — mint `ADMIN_PASSWORD_HASH`.

```
uv run python -m fleetforge.auth hash-password           # prompts twice (getpass)
uv run python -m fleetforge.auth hash-password --stdin   # reads one line (scriptable)
```

**stdout carries the PHC string and nothing else**, so it can be piped. The hint
about `.env` goes to stderr. The password is never echoed, never logged and never
written to a file — pasting the printed line into `.env` is the operator's job.

The single quotes in that hint are load-bearing: an argon2 PHC string is full of
`$`, and docker compose interpolates `$argon2id` / `$v` / `$m` as variables, leaving
a silently truncated hash and a login that can never succeed. `python-dotenv` strips
the surrounding quotes, so one single-quoted line serves both the host process and
compose interpolation.
"""

import argparse
import asyncio
import datetime as dt
import getpass
import json
import sys

from sqlalchemy import select, text

from fleetforge.auth.hashing import hash_secret
from fleetforge.auth.tokens import ADMIN_TOKEN_PREFIX, issue_token
from fleetforge.clock import now_utc
from fleetforge.db.base import get_engine, get_sessionmaker
from fleetforge.db.models import AdminToken, Project, ProjectMember, ProjectRole, User

ENV_HINT = (
    "\nPaste this into .env / .env.example, SINGLE-QUOTED exactly as shown "
    "(docker compose eats the $ segments otherwise):\n\n"
    "  ADMIN_PASSWORD_HASH='{phc}'\n"
)


def _read_password(from_stdin: bool) -> str:
    if from_stdin:
        return sys.stdin.readline().rstrip("\n")
    password = getpass.getpass("Admin password: ")
    if password != getpass.getpass("Repeat: "):
        raise SystemExit("passwords do not match")
    return password


async def _issue_token_db(
    *,
    name: str,
    subject: str,
    project_slug: str | None,
    scopes: list[str],
    expires_in_days: int | None,
) -> str:
    issued = issue_token(ADMIN_TOKEN_PREFIX)
    now = now_utc()
    expires_at = now + dt.timedelta(days=expires_in_days) if expires_in_days is not None else None
    sessionmaker = get_sessionmaker()
    try:
        async with sessionmaker() as session:
            project_id = None
            if project_slug is not None:
                proj = await session.scalar(select(Project).where(Project.slug == project_slug))
                if proj is None:
                    raise SystemExit(f"project {project_slug!r} not found")
                project_id = proj.id
            session.add(
                AdminToken(
                    id=issued.token_id,
                    name=name,
                    secret_hash=issued.secret_hash,
                    subject=subject,
                    scopes=scopes,
                    project_id=project_id,
                    created_at=now,
                    expires_at=expires_at,
                )
            )
            await session.commit()
    finally:
        await get_engine().dispose()
    return issued.token


async def _create_user_db(
    *,
    username: str,
    email: str | None,
    display_name: str | None,
    password_hash: str | None,
    is_superadmin: bool,
    project_slug: str | None = None,
    role: str = ProjectRole.OPERATOR.value,
) -> str:
    now = now_utc()
    sessionmaker = get_sessionmaker()
    try:
        async with sessionmaker() as session:
            target_proj: Project | None = None
            if project_slug is not None:
                target_proj = await session.scalar(
                    select(Project).where(Project.slug == project_slug)
                )
                if target_proj is None:
                    raise SystemExit(f"project {project_slug!r} not found")
            user = User(
                username=username,
                email=email.strip().lower() if email else None,
                display_name=display_name,
                password_hash=password_hash,
                is_superadmin=is_superadmin,
                created_at=now,
                updated_at=now,
            )
            session.add(user)
            await session.flush()
            if target_proj is not None:
                session.add(
                    ProjectMember(
                        project_id=target_proj.id,
                        user_id=user.id,
                        role=role,
                        created_at=now,
                    )
                )
            await session.commit()
            return str(user.id)
    finally:
        await get_engine().dispose()


async def _update_user_db(
    *,
    username: str,
    email: str | None,
    display_name: str | None,
    password_hash: str | None,
    is_superadmin: bool | None,
    disabled: bool | None,
    project_slugs: list[str],
    role: str,
) -> dict[str, object]:
    now = now_utc()
    sessionmaker = get_sessionmaker()
    try:
        async with sessionmaker() as session:
            user = await session.scalar(select(User).where(User.username == username))
            if user is None:
                raise SystemExit(f"user {username!r} not found")
            if email is not None:
                user.email = email.strip().lower() or None
            if display_name is not None:
                user.display_name = display_name.strip() or None
            if password_hash is not None:
                user.password_hash = password_hash
            if is_superadmin is not None:
                user.is_superadmin = is_superadmin
            if disabled is not None:
                if disabled:
                    user.disabled_at = now
                    await session.execute(
                        text(
                            "UPDATE admin_tokens SET revoked_at = now() "
                            "WHERE user_id = :user_id AND revoked_at IS NULL"
                        ),
                        {"user_id": user.id},
                    )
                else:
                    user.disabled_at = None
            user.updated_at = now
            for slug in project_slugs:
                proj = await session.scalar(select(Project).where(Project.slug == slug))
                if proj is None:
                    raise SystemExit(f"project {slug!r} not found")
                existing = await session.get(ProjectMember, (proj.id, user.id))
                if existing is not None:
                    existing.role = role
                else:
                    session.add(
                        ProjectMember(
                            project_id=proj.id,
                            user_id=user.id,
                            role=role,
                            created_at=now,
                        )
                    )
            await session.commit()
            return {
                "id": str(user.id),
                "username": user.username,
                "email": user.email,
                "display_name": user.display_name,
                "has_local_password": user.password_hash is not None,
                "is_superadmin": user.is_superadmin,
                "disabled_at": user.disabled_at.isoformat() if user.disabled_at else None,
            }
    finally:
        await get_engine().dispose()


async def _list_users_db() -> list[dict[str, object]]:
    sessionmaker = get_sessionmaker()
    try:
        async with sessionmaker() as session:
            users = (await session.scalars(select(User).order_by(User.username))).all()
            members = (
                await session.execute(
                    select(ProjectMember.user_id, Project.slug, ProjectMember.role)
                    .join(Project, Project.id == ProjectMember.project_id)
                    .order_by(Project.slug)
                )
            ).all()
            proj_by_user: dict[object, list[dict[str, str]]] = {}
            for uid, slug, role in members:
                proj_by_user.setdefault(uid, []).append({"project": slug, "role": role})
            return [
                {
                    "id": str(u.id),
                    "username": u.username,
                    "email": u.email,
                    "display_name": u.display_name,
                    "firebase_uid": u.firebase_uid,
                    "has_local_password": u.password_hash is not None,
                    "is_superadmin": u.is_superadmin,
                    "disabled_at": u.disabled_at.isoformat() if u.disabled_at else None,
                    "projects": proj_by_user.get(u.id, []),
                }
                for u in users
            ]
    finally:
        await get_engine().dispose()


def main(argv: list[str] | None = None) -> int:
    """Run auth CLI subcommands (`hash-password`, `issue-token`, `create-user`, `update-user`, `list-users`)."""
    parser = argparse.ArgumentParser(prog="python -m fleetforge.auth")
    subparsers = parser.add_subparsers(dest="command", required=True)
    hash_parser = subparsers.add_parser(
        "hash-password", help="argon2id-hash an admin password for ADMIN_PASSWORD_HASH"
    )
    hash_parser.add_argument(
        "--stdin", action="store_true", help="read the password from stdin instead of prompting"
    )

    token_parser = subparsers.add_parser(
        "issue-token", help="mint a named or project-scoped ffa_ token in the database"
    )
    token_parser.add_argument("--name", required=True, help="token label (e.g. github-actions)")
    token_parser.add_argument("--project", default=None, help="optional project slug to pin to")
    token_parser.add_argument(
        "--scope",
        dest="scopes",
        action="append",
        choices=["admin", "read", "write:artifacts", "deploy", "enroll"],
        help="token scope (repeatable; defaults to admin)",
    )
    token_parser.add_argument("--subject", default="admin", help="token subject")
    token_parser.add_argument("--expires-in-days", type=int, default=None, help="optional TTL")

    user_parser = subparsers.add_parser(
        "create-user", help="invite or create a human operator account in the database"
    )
    user_parser.add_argument("--username", required=True, help="lowercase handle")
    user_parser.add_argument(
        "--email", default=None, help="email address (required for Firebase Auth)"
    )
    user_parser.add_argument("--display-name", default=None, help="human name")
    user_parser.add_argument("--superadmin", action="store_true", help="grant superadmin access")
    user_parser.add_argument("--project", default=None, help="optional project slug to grant")
    user_parser.add_argument(
        "--role",
        default=ProjectRole.OPERATOR.value,
        choices=[r.value for r in ProjectRole],
        help="project role when --project is specified (default: operator)",
    )
    user_parser.add_argument(
        "--no-password",
        action="store_true",
        help="invite user via Firebase email without setting a local password",
    )
    user_parser.add_argument(
        "--stdin", action="store_true", help="read the password from stdin instead of prompting"
    )

    update_parser = subparsers.add_parser(
        "update-user",
        help="reset password, set display name/email, grant superadmin, or assign project roles",
    )
    update_parser.add_argument("--username", required=True, help="lowercase handle to update")
    update_parser.add_argument("--email", default=None, help="update email address")
    update_parser.add_argument("--display-name", default=None, help="update human display name")
    super_group = update_parser.add_mutually_exclusive_group()
    super_group.add_argument(
        "--superadmin",
        dest="is_superadmin",
        action="store_true",
        default=None,
        help="grant superadmin access (full capability across all projects and users)",
    )
    super_group.add_argument(
        "--no-superadmin",
        dest="is_superadmin",
        action="store_false",
        help="revoke superadmin access",
    )
    disable_group = update_parser.add_mutually_exclusive_group()
    disable_group.add_argument(
        "--disable",
        dest="disabled",
        action="store_true",
        default=None,
        help="disable the user and revoke active tokens",
    )
    disable_group.add_argument(
        "--enable",
        dest="disabled",
        action="store_false",
        help="re-enable a disabled user",
    )
    update_parser.add_argument(
        "--set-password",
        action="store_true",
        help="prompt for (or read with --stdin) a new local password",
    )
    update_parser.add_argument(
        "--stdin",
        action="store_true",
        help="read the new password from stdin (implies --set-password)",
    )
    update_parser.add_argument(
        "--project",
        dest="projects",
        action="append",
        default=[],
        help="assign user to project slug (repeatable)",
    )
    update_parser.add_argument(
        "--role",
        default=ProjectRole.OWNER.value,
        choices=[r.value for r in ProjectRole],
        help="project role when --project is specified (default: owner)",
    )

    subparsers.add_parser("list-users", help="list all human users and their project roles as JSON")

    args = parser.parse_args(argv)

    if args.command == "hash-password":
        password = _read_password(args.stdin)
        if not password:
            raise SystemExit("empty password")
        phc = hash_secret(password)
        print(phc)
        print(ENV_HINT.format(phc=phc), file=sys.stderr)
        return 0

    if args.command == "issue-token":
        scopes = args.scopes or ["admin"]
        token = asyncio.run(
            _issue_token_db(
                name=args.name,
                subject=args.subject,
                project_slug=args.project,
                scopes=scopes,
                expires_in_days=args.expires_in_days,
            )
        )
        print(token)
        return 0

    if args.command == "create-user":
        phc_opt: str | None = None
        if args.no_password:
            if not args.email:
                raise SystemExit("--no-password requires --email")
        else:
            password = _read_password(args.stdin)
            if not password:
                raise SystemExit("empty password")
            phc_opt = hash_secret(password)
        uid = asyncio.run(
            _create_user_db(
                username=args.username,
                email=args.email,
                display_name=args.display_name,
                password_hash=phc_opt,
                is_superadmin=bool(args.superadmin),
                project_slug=args.project,
                role=args.role,
            )
        )
        print(uid)
        return 0

    if args.command == "update-user":
        update_phc: str | None = None
        if args.set_password or args.stdin:
            password = _read_password(args.stdin)
            if not password:
                raise SystemExit("empty password")
            update_phc = hash_secret(password)
        updated = asyncio.run(
            _update_user_db(
                username=args.username,
                email=args.email,
                display_name=args.display_name,
                password_hash=update_phc,
                is_superadmin=args.is_superadmin,
                disabled=args.disabled,
                project_slugs=list(args.projects),
                role=args.role,
            )
        )
        print(json.dumps(updated))
        return 0

    if args.command == "list-users":
        users = asyncio.run(_list_users_db())
        print(json.dumps(users, indent=2))
        return 0

    return 1


if __name__ == "__main__":
    raise SystemExit(main())

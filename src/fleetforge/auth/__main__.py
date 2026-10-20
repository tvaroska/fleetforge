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
import sys

from sqlalchemy import select

from fleetforge.auth.hashing import hash_secret
from fleetforge.auth.tokens import ADMIN_TOKEN_PREFIX, issue_token
from fleetforge.clock import now_utc
from fleetforge.db.base import get_engine, get_sessionmaker
from fleetforge.db.models import AdminToken, Project, User

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
) -> str:
    now = now_utc()
    sessionmaker = get_sessionmaker()
    try:
        async with sessionmaker() as session:
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
            await session.commit()
            return str(user.id)
    finally:
        await get_engine().dispose()


def main(argv: list[str] | None = None) -> int:
    """Run auth CLI subcommands (`hash-password`, `issue-token`, `create-user`)."""
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
    user_parser.add_argument(
        "--no-password",
        action="store_true",
        help="invite user via Firebase email without setting a local password",
    )
    user_parser.add_argument(
        "--stdin", action="store_true", help="read the password from stdin instead of prompting"
    )

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
            )
        )
        print(uid)
        return 0

    return 1


if __name__ == "__main__":
    raise SystemExit(main())

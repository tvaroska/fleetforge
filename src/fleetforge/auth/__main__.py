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
import getpass
import sys

from fleetforge.auth.hashing import hash_secret

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


def main(argv: list[str] | None = None) -> int:
    """Hash a password and print the PHC string. Returns a process exit code."""
    parser = argparse.ArgumentParser(prog="python -m fleetforge.auth")
    subparsers = parser.add_subparsers(dest="command", required=True)
    hash_parser = subparsers.add_parser(
        "hash-password", help="argon2id-hash an admin password for ADMIN_PASSWORD_HASH"
    )
    hash_parser.add_argument(
        "--stdin", action="store_true", help="read the password from stdin instead of prompting"
    )
    args = parser.parse_args(argv)

    password = _read_password(args.stdin)
    if not password:
        raise SystemExit("empty password")

    phc = hash_secret(password)
    print(phc)
    print(ENV_HINT.format(phc=phc), file=sys.stderr)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

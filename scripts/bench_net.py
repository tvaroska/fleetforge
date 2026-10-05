#!/usr/bin/env python3
"""Known-networks bench checker: is the board online on the expected network? (R2b-test-4)

The bench moves one board between two Wi-Fi networks (docs/runbooks/known-networks-bench.md).
This script grades each step from the server's own record, the device row, so "the row
follows the board" is a verdict and not an operator's impression. The server keeps no SSID
history, so `watch` polls the row and prints each change. Stdlib only, no project venv,
no credentials: the SELECT runs in psql, over ssh for prod. Nothing but the device row's
own fields is read, and no passphrase is in that table.

    python3 scripts/bench_net.py --sql DEVICE                      # the SELECT, on stdout
    python3 scripts/bench_net.py check --ssid S --known N < row     # grade one psql row
    python3 scripts/bench_net.py watch DEVICE --ssid S --known N [--where prod|dev]
                                     [--timeout SECONDS] [--interval SECONDS]

DEVICE is 12 hex (any case). Anything else is refused before any SQL exists or any process
is spawned: the SQL travels through ssh. The SSID is compared in Python only, exactly
(case-sensitive, byte for byte, as the agent's strcmp does), and never enters SQL.
`--timeout 0` reads once and grades once. Exit codes: 0 PASS, 1 FAIL, 2 usage / refused
device / no such device / fetch error, 3 WAIT (and `watch` timing out while still waiting).
"""

import argparse
import re
import subprocess
import sys
import time
from collections.abc import Callable
from dataclasses import dataclass
from pathlib import Path

EXIT_PASS = 0
EXIT_FAIL = 1
EXIT_USAGE = 2
EXIT_WAIT = 3

DEVICE_RE = re.compile(r"^[0-9a-f]{12}$")
VERSION_RE = re.compile(r"^(\d+)\.(\d+)\.(\d+)")
# The first agent that reads `nets` and selects among them (R2b-fw-1).
MIN_AGENT = (0, 4, 6)
MAX_KNOWN = 4  # the protocol cap
REPO_ROOT = Path(__file__).resolve().parent.parent
FETCH_TIMEOUT_S = 60

# Same remote string as the justfile's bench-judge recipe: double quotes around the `|`.
PROD_REMOTE = (
    "cd /opt/boris/prod && docker compose exec -T postgres psql -U fleetforge fleetforge "
    '-v ON_ERROR_STOP=1 -At -F "|"'
)
DEV_ARGV = [
    "docker",
    "compose",
    "exec",
    "-T",
    "postgres",
    "psql",
    "-U",
    "fleetforge",
    "fleetforge",
    "-v",
    "ON_ERROR_STOP=1",
    "-At",
    "-F",
    "|",
]

NO_SSID_COLUMN = "this server has no ssid column: the R2b server (migration 0005) is not on it"


class BenchError(Exception):
    """A usage, refused-device or fetch error. `main` prints it and exits 2."""


@dataclass(frozen=True)
class FetchResult:
    returncode: int
    stdout: str
    stderr: str


Fetch = Callable[[str], FetchResult]


@dataclass(frozen=True)
class Reading:
    """The device row, as psql printed it. `ssid` is the last column."""

    device_id: str
    power_class: str
    presence: str  # 'true' | 'false' | ''
    agent_version: str
    link_type: str
    known: int | None
    age_s: int | None
    ssid: str | None

    @property
    def online(self) -> bool:
        # presence.py::is_online for always_on: presence_reported is true.
        return self.presence == "true"


@dataclass(frozen=True)
class Verdict:
    code: int
    status: str  # the fleet row's own words, with the last-seen age
    key: str  # the status without the age: what counts as "changed"
    final: str  # NET PASS|FAIL|WAIT ...
    reason: str  # the bare reason (for NET TIMEOUT)
    warnings: tuple[str, ...] = ()


def validate_device(text: str) -> str:
    device = text.lower()
    if not DEVICE_RE.fullmatch(device):
        raise BenchError(f"refused device {text!r}: a device id is 12 hex characters")
    return device


def build_sql(device: str) -> str:
    device = validate_device(device)
    return (
        "SELECT device_id, power_class, coalesce(presence_reported::text, ''), "  # noqa: S608
        "coalesce(agent_version, ''), link_type, coalesce(known_networks::text, ''), "
        "coalesce(floor(extract(epoch FROM now() - last_seen))::bigint::text, ''), "
        "coalesce(ssid, '') "
        f"FROM devices WHERE device_id = '{device}' AND decommissioned_at IS NULL;"
    )


def parse_reading(text: str) -> Reading | None:
    """The first non-empty line of `psql -At -F '|'` output; None when there is no row."""
    line = next((ln for ln in text.splitlines() if ln.strip()), None)
    if line is None:
        return None
    fields = line.split("|", 7)  # ssid is last: a `|` inside an SSID survives
    if len(fields) != 8:
        raise BenchError(f"unreadable row: expected 8 fields, got {len(fields)}")
    device_id, power_class, presence, version, link, known, age, ssid = fields
    try:
        known_n = int(known) if known else None
        age_n = int(age) if age else None
    except ValueError as exc:
        raise BenchError(f"unreadable row: {exc}") from None
    return Reading(
        device_id=device_id,
        power_class=power_class,
        presence=presence,
        agent_version=version,
        link_type=link,
        known=known_n,
        age_s=age_n,
        ssid=ssid or None,
    )


def parse_version(text: str) -> tuple[int, int, int] | None:
    m = VERSION_RE.match(text)
    return (int(m[1]), int(m[2]), int(m[3])) if m else None


def _plural(n: int) -> str:
    return f"{n} network" if n == 1 else f"{n} networks"


def _status(r: Reading, *, with_age: bool) -> str:
    if r.ssid is None:
        where = "online, no network reported" if r.online else "offline, no network reported"
    elif r.online:
        where = f'online, on: "{r.ssid}"'
    else:
        where = f'offline, last on: "{r.ssid}"'
    knows = f"knows {_plural(r.known)}" if r.known is not None else "known networks not reported"
    version = r.agent_version or "unknown"
    line = f"{r.device_id}  {where}  {knows}  agent {version}"
    if with_age:
        age = f"{r.age_s} s ago" if r.age_s is not None else "never"
        line += f"  last seen {age}"
    return line


def grade(r: Reading, ssid: str, known: int) -> Verdict:
    """The verdict of one read. FAIL means waiting will not fix it."""
    warnings: list[str] = []
    status = _status(r, with_age=True)
    key = _status(r, with_age=False)

    def verdict(code: int, word: str, why: str) -> Verdict:
        return Verdict(code, status, key, f"NET {word} {why}", why, tuple(warnings))

    # Wrong board or wrong flash.
    if r.link_type != "wifi":
        return verdict(EXIT_FAIL, "FAIL", f"link type is {r.link_type!r}, not wifi")
    version = parse_version(r.agent_version)
    if version is None:
        warnings.append(f"WARN agent version {r.agent_version!r} is unparseable: gate skipped")
    elif version < MIN_AGENT:
        return verdict(
            EXIT_FAIL,
            "FAIL",
            f"agent {r.agent_version} joins only network 1; known networks need 0.4.6 or later",
        )
    if r.known is not None and r.known != known:
        return verdict(
            EXIT_FAIL,
            "FAIL",
            f"the board reports {r.known} known networks, expected {known}: re-flash with {known}",
        )
    if r.power_class != "always_on":
        warnings.append("WARN sleepy board: presence is not graded here")

    # Right board, not there yet.
    if not r.online:
        last = f' (last on "{r.ssid}")' if r.ssid else ""
        return verdict(EXIT_WAIT, "WAIT", f"the board is offline{last}")
    if r.ssid != ssid:
        on = f'"{r.ssid}"' if r.ssid else "no network reported"
        return verdict(EXIT_WAIT, "WAIT", f'the board is online on {on}, expected "{ssid}"')
    if r.known is None:
        return verdict(EXIT_FAIL, "FAIL", "the board does not report known_networks")
    return verdict(EXIT_PASS, "PASS", f'on "{ssid}" (known network count {known})')


def read_device(fetch: Fetch, device: str) -> Reading:
    """Fetch and parse one device row; BenchError on any failure or no row."""
    result = fetch(build_sql(device))
    if result.returncode != 0:
        stderr = result.stderr
        if 'column "ssid" does not exist' in stderr or 'column "known_networks"' in stderr:
            raise BenchError(NO_SSID_COLUMN)
        first = next((ln for ln in stderr.splitlines() if ln.strip()), "no message")
        raise BenchError(f"fetch failed (exit {result.returncode}): {first}")
    reading = parse_reading(result.stdout)
    if reading is None:
        raise BenchError(f"no live device {device} on this server")
    return reading


def _run(argv: list[str], sql: str) -> FetchResult:
    try:
        done = subprocess.run(  # noqa: S603 (an argv list, never a shell; SQL on stdin)
            argv,
            input=sql,
            capture_output=True,
            text=True,
            cwd=REPO_ROOT,
            timeout=FETCH_TIMEOUT_S,
            check=False,
        )
    except subprocess.TimeoutExpired:
        return FetchResult(124, "", f"timed out after {FETCH_TIMEOUT_S} s")
    except OSError as exc:
        return FetchResult(127, "", str(exc))
    return FetchResult(done.returncode, done.stdout, done.stderr)


def fetch_prod(sql: str) -> FetchResult:
    return _run(["ssh", "prod", PROD_REMOTE], sql)


def fetch_dev(sql: str) -> FetchResult:
    return _run(DEV_ARGV, sql)


def _stamp() -> str:
    return time.strftime("%H:%M:%S")


def watch(
    fetch: Fetch,
    device: str,
    ssid: str,
    known: int,
    *,
    timeout: int,
    interval: int,
    sleep: Callable[[float], None] = time.sleep,
    monotonic: Callable[[], float] = time.monotonic,
    stamp: Callable[[], str] = _stamp,
) -> int:
    """Poll until PASS/FAIL or the deadline. Prints the status line only when it changes."""
    deadline = monotonic() + timeout
    last_key: str | None = None
    warned: set[str] = set()
    while True:
        try:
            v = grade(read_device(fetch, device), ssid, known)
        except BenchError as exc:
            print(exc, file=sys.stderr)
            return EXIT_USAGE
        for w in v.warnings:
            if w not in warned:
                warned.add(w)
                print(w)
        if v.key != last_key:
            last_key = v.key
            print(f"{stamp()}  {v.status}")
        if v.code != EXIT_WAIT:
            print(v.final)
            return v.code
        if timeout == 0:
            print(v.final)
            return EXIT_WAIT
        remaining = deadline - monotonic()
        if remaining <= 0:
            print(f"NET TIMEOUT after {timeout} s: {v.reason}")
            return EXIT_WAIT
        sleep(min(interval, remaining))


def _known(text: str) -> int:
    try:
        n = int(text)
    except ValueError:
        raise argparse.ArgumentTypeError(f"{text!r} is not a whole number") from None
    if not 1 <= n <= MAX_KNOWN:
        raise argparse.ArgumentTypeError(f"must be 1..{MAX_KNOWN}")
    return n


def _seconds(minimum: int) -> Callable[[str], int]:
    def parse(text: str) -> int:
        try:
            n = int(text)
        except ValueError:
            raise argparse.ArgumentTypeError(f"{text!r} is not a whole number") from None
        if n < minimum:
            raise argparse.ArgumentTypeError(f"must be at least {minimum}")
        return n

    return parse


def _ssid(text: str) -> str:
    if not text:
        raise argparse.ArgumentTypeError("the expected SSID must not be empty")
    return text


def _expect(p: argparse.ArgumentParser) -> None:
    p.add_argument("--ssid", required=True, type=_ssid, help="the expected network, exactly")
    p.add_argument("--known", required=True, type=_known, help="the expected known-network count")


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description="Grade a board's device row against the expected network (R2b-test-4)."
    )
    parser.add_argument("--sql", metavar="DEVICE", help="print the SELECT for DEVICE and exit")
    sub = parser.add_subparsers(dest="command")
    check = sub.add_parser("check", help="grade one psql row read from stdin")
    _expect(check)
    w = sub.add_parser("watch", help="poll the device row until it passes")
    w.add_argument("device", help="12 hex device id")
    _expect(w)
    w.add_argument("--where", choices=("prod", "dev"), default="prod")
    w.add_argument(
        "--timeout",
        type=_seconds(0),
        default=300,
        help="seconds to keep polling; 0 reads once (default 300)",
    )
    w.add_argument("--interval", type=_seconds(1), default=5, help="seconds between polls")
    return parser


def main(argv: list[str] | None = None) -> int:
    parser = build_parser()
    args = parser.parse_args(argv)
    try:
        if args.sql is not None:
            print(build_sql(args.sql))
            return EXIT_PASS
        if args.command == "check":
            reading = parse_reading(sys.stdin.read())
            if reading is None:
                raise BenchError("no live device on this server")
            v = grade(reading, args.ssid, args.known)
            for w in v.warnings:
                print(w)
            print(v.status)
            print(v.final)
            return v.code
        if args.command == "watch":
            device = validate_device(args.device)  # before anything is spawned
            fetch = fetch_prod if args.where == "prod" else fetch_dev
            return watch(
                fetch,
                device,
                args.ssid,
                args.known,
                timeout=args.timeout,
                interval=args.interval,
            )
    except BenchError as exc:
        print(exc, file=sys.stderr)
        return EXIT_USAGE
    parser.print_usage(sys.stderr)
    return EXIT_USAGE


if __name__ == "__main__":
    sys.exit(main())

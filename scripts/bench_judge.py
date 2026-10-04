#!/usr/bin/env python3
"""Bench-replay judge: grade one deploy transaction's `deploy_events` rows (R2b-test-2).

The R2 recovery paths (boot loop, power cut, hang, stall, radio outages) were proven in
QEMU; the bench replay re-runs them on the ESP32-S3 and this script grades each step from
the server's own record, so a pass is the rows' shape and not an operator's impression.
Stdlib only, no project venv, no credentials. See docs/runbooks/bench-replay.md.

    python3 scripts/bench_judge.py --sql REF        # the SELECT for REF, on stdout
    python3 scripts/bench_judge.py SCENARIO < rows   # grade psql -At -F '|' output
    python3 scripts/bench_judge.py --list            # the scenarios and their bench steps

REF is a cmd_id (32 hex) or a device id (12 hex: that device's newest transaction).
Anything else is refused before any SQL is produced: the SQL travels through a shell
and ssh. Exit codes: 0 PASS, 1 FAIL, 2 usage / refused REF / no rows, 3 INCOMPLETE.

The rows are printed back as text. They carry no URL (the writer redacts them).
"""

import argparse
import re
import sys
from dataclasses import dataclass, field

EXIT_PASS = 0
EXIT_FAIL = 1
EXIT_USAGE = 2
EXIT_INCOMPLETE = 3

CMD_RE = re.compile(r"^[0-9a-f]{32}$")
DEVICE_RE = re.compile(r"^[0-9a-f]{12}$")
VERSION_RE = re.compile(r"^(\d+)\.(\d+)\.(\d+)")
# The real agent's wording (agent/main/ff_txn.c). The simulator says "returned to <ver>;
# the new image did not confirm", which must not pass: the bench grades the agent.
ROLLBACK_DETAIL_RE = re.compile(r"^returned to ota_\d+; ota_\d+ did not confirm")
TEST_SUFFIXES = ("-rbtest", "-bltest", "-hangtest")
# agent/main/ff_ota.c::fail(...). Exact match: "download" alone is not a stall.
DOWNLOAD_FAILURES = ("download stalled", "download failed")

SELECT_COLUMNS = (
    "SELECT extract(epoch FROM at), cmd_id, state, is_terminal, coalesce(from_version, ''), "
    "coalesce(artifact_version, ''), coalesce(detail->>'detail', '') FROM deploy_events "
)

PASS = "PASS"  # noqa: S105 (a verdict word, not a password)
FAIL = "FAIL"
WARN = "WARN"

Version = tuple[int, int, int]


@dataclass(frozen=True)
class Row:
    """One `deploy_events` row, as psql printed it."""

    at: float
    cmd_id: str
    state: str
    terminal: bool
    from_version: str
    artifact_version: str
    detail: str


@dataclass
class Check:
    """One graded line of the verdict."""

    name: str
    result: str
    why: str


@dataclass(frozen=True)
class Scenario:
    """The pass shape of one bench step. One generic checker reads these."""

    name: str
    step: str
    # Required test suffix on artifact_version; None means a normal image.
    suffix: str | None
    # Must appear in this order (subsequence, not equality).
    order: tuple[str, ...] = ()
    forbidden: tuple[str, ...] = ()
    # Allowed terminal (state, exact details or None for any detail).
    terminal: tuple[tuple[str, tuple[str, ...] | None], ...] = ()
    rollback_detail: bool = False
    # Optional rows whose absence is a WARN, not a FAIL.
    best_effort: tuple[str, ...] = ()
    min_from: Version | None = None
    min_artifact: Version | None = None
    # Seconds from t_ref (rebooting, else staged) to the terminal row.
    window_min: float | None = None
    window_max: float | None = None
    window_max_warn_only: bool = False
    window_needs_rebooting: bool = False
    # power-cut: the last row must be one of these and not terminal.
    open_end: tuple[str, ...] = ()
    notes: tuple[str, ...] = field(default_factory=tuple)


SCENARIOS: dict[str, Scenario] = {
    s.name: s
    for s in (
        Scenario(
            name="confirmed",
            step="0 baseline; 2 power-cut re-deploy (same cmd); radio R3 short outage at rebooting",
            suffix=None,
            order=("staged", "confirming", "confirmed"),
            forbidden=("rolled_back", "failed"),
            terminal=(("confirmed", None),),
        ),
        Scenario(
            name="rbtest",
            step="1 rbtest: boots, joins, never confirms; the 60 s timer rolls it back",
            suffix="-rbtest",
            order=("staged", "confirming", "rolled_back"),
            terminal=(("rolled_back", None),),
            rollback_detail=True,
            best_effort=("rolling_back",),
            min_from=(0, 4, 0),
            window_min=60.0,
            window_max=240.0,
            window_needs_rebooting=True,
        ),
        Scenario(
            name="bootloop",
            step="3 boot loop: aborts before the session; the bootloader puts the old slot back",
            suffix="-bltest",
            order=("staged", "rolled_back"),
            forbidden=("confirming", "rolling_back"),
            terminal=(("rolled_back", None),),
            rollback_detail=True,
            min_from=(0, 4, 0),
            window_max=180.0,
            window_needs_rebooting=True,
            notes=("the console must show `abort() was called` exactly once",),
        ),
        Scenario(
            name="hang",
            step="4 hang before the session; the 300 s OTA-boot timer rolls it back",
            suffix="-hangtest",
            order=("staged", "rolled_back"),
            forbidden=("confirming", "rolling_back"),
            terminal=(("rolled_back", None),),
            rollback_detail=True,
            min_from=(0, 4, 0),
            min_artifact=(0, 4, 3),
            window_min=300.0,
            window_max=900.0,
            window_max_warn_only=True,
        ),
        Scenario(
            name="power-cut",
            step="2 power cut: pull USB at `30%`; the transaction stays open (deploys.py rule 1)",
            suffix=None,
            forbidden=("staged", "confirming", "rolled_back", "failed"),
            open_end=("downloading", "verifying"),
        ),
        Scenario(
            name="outage-short",
            step="5 radio R1: AP off 30 s mid-download; the deploy must not park",
            suffix=None,
            terminal=(("confirmed", None), ("failed", DOWNLOAD_FAILURES)),
            min_from=(0, 4, 4),
        ),
        Scenario(
            name="stall",
            step="5 radio R2: AP off 6 min; S: WAN cut with the AP up, 6 min",
            suffix=None,
            forbidden=("staged",),
            terminal=(("failed", DOWNLOAD_FAILURES),),
            min_from=(0, 4, 4),
            notes=(
                "`download failed` = keepalive/close got there first (the expected metal outcome);"
                " `download stalled` = the R2-fw-5 guard",
                "still `downloading` ~100 s after the outage is the pre-0.4.4 regression:"
                " that INCOMPLETE never resolves",
            ),
        ),
        Scenario(
            name="reboot-outage-long",
            step="5 radio R4: AP off 6 min right after `rebooting`; a good image rolls back",
            suffix=None,
            forbidden=("confirming",),
            terminal=(("rolled_back", None),),
            rollback_detail=True,
            min_from=(0, 4, 0),
            window_min=300.0,
            notes=(
                "a good image rolled back is a miss, not a brick (DECISIONS 2026-10-03 R2-fw-4)",
            ),
        ),
    )
}


class RefError(ValueError):
    """REF is neither a cmd_id nor a device id."""


def build_sql(ref: str) -> str:
    """The one SELECT for REF. Refuses anything not hex-shaped before building it."""
    ref = ref.strip().lower()
    if CMD_RE.match(ref):
        where = f"WHERE cmd_id = '{ref}' "
    elif DEVICE_RE.match(ref):
        where = (
            # ref is 12 hex by the regex above: nothing else reaches the SQL.
            "WHERE cmd_id = (SELECT cmd_id FROM deploy_events "  # noqa: S608
            f"WHERE device_id = '{ref}' AND state = 'requested' "
            "ORDER BY at DESC, id DESC LIMIT 1) "
        )
    else:
        raise RefError(f"refused REF {ref!r}: expected a cmd_id (32 hex) or a device id (12 hex)")
    return SELECT_COLUMNS + where + "ORDER BY at, id;"


def parse_rows(text: str) -> list[Row]:
    """Parse `psql -At -F '|'` output. A `|` inside detail is kept whole."""
    rows = []
    for line in text.splitlines():
        if not line.strip():
            continue
        parts = line.split("|", 6)
        if len(parts) != 7:
            raise ValueError(f"unreadable row (want 7 fields): {line!r}")
        at, cmd_id, state, term, from_v, art_v, detail = parts
        if term not in ("t", "f"):
            raise ValueError(f"unreadable is_terminal {term!r} in row: {line!r}")
        rows.append(
            Row(
                at=float(at),
                cmd_id=cmd_id,
                state=state,
                terminal=term == "t",
                from_version=from_v,
                artifact_version=art_v,
                detail=detail,
            )
        )
    return rows


def parse_version(text: str) -> Version | None:
    """`0.4.5-rbtest` -> (0, 4, 5); None if it does not start with three numbers."""
    m = VERSION_RE.match(text)
    if not m:
        return None
    return int(m.group(1)), int(m.group(2)), int(m.group(3))


def is_subsequence(wanted: tuple[str, ...], states: list[str]) -> bool:
    """True if `wanted` appears in `states` in order, gaps allowed."""
    it = iter(states)
    return all(any(s == w for s in it) for w in wanted)


def _fmt(v: Version) -> str:
    return ".".join(str(n) for n in v)


def _suffix_of(version: str) -> str | None:
    for suffix in TEST_SUFFIXES:
        if version.endswith(suffix):
            return suffix
    return None


def _last_at(rows: list[Row], state: str) -> float | None:
    times = [r.at for r in rows if r.state == state]
    return times[-1] if times else None


def judge(scenario: Scenario, rows: list[Row]) -> tuple[str, list[Check]]:
    """Grade rows against a scenario. Returns (PASS|FAIL|INCOMPLETE, checks)."""
    checks: list[Check] = []

    def add(name: str, ok: bool, why: str, soft: bool = False) -> None:
        checks.append(Check(name, PASS if ok else (WARN if soft else FAIL), why))

    states = [r.state for r in rows]
    first = rows[0]
    last = rows[-1]
    add(
        "requested-first",
        first.state == "requested",
        "the transaction opens with our `requested` row"
        if first.state == "requested"
        else f"first row is {first.state!r}: not a transaction this server sent",
    )

    head = first if first.state == "requested" else last
    artifact = head.artifact_version or last.artifact_version
    from_v = head.from_version or last.from_version

    # Test suffix: the artifact must be the scenario's image.
    art_suffix = _suffix_of(artifact)
    if scenario.suffix is None:
        add(
            "image",
            art_suffix is None,
            f"normal image {artifact!r}"
            if art_suffix is None
            else f"{artifact!r} is a {art_suffix} image; this step wants a normal one",
        )
    else:
        add(
            "image",
            art_suffix == scenario.suffix,
            f"{artifact!r} ends with {scenario.suffix}"
            if art_suffix == scenario.suffix
            else f"{artifact!r} does not end with {scenario.suffix}",
        )
    from_suffix = _suffix_of(from_v)
    if from_suffix is not None:
        add(
            "from-image",
            False,
            f"from {from_v!r}: board was not on a normal image before the run",
            soft=True,
        )

    # Version gates.
    if scenario.min_from is not None:
        fv = parse_version(from_v)
        if fv is None:
            add("from-version", False, f"cannot parse from_version {from_v!r}; skipped", soft=True)
        else:
            add(
                "from-version",
                fv >= scenario.min_from,
                f"running image {from_v} >= {_fmt(scenario.min_from)}"
                if fv >= scenario.min_from
                else f"running image {from_v} < {_fmt(scenario.min_from)}: it cannot do this step",
            )
    if scenario.min_artifact is not None:
        av = parse_version(artifact)
        if av is None:
            add("artifact-version", False, f"cannot parse {artifact!r}; skipped", soft=True)
        else:
            add(
                "artifact-version",
                av >= scenario.min_artifact,
                f"{artifact} >= {_fmt(scenario.min_artifact)}"
                if av >= scenario.min_artifact
                else f"{artifact} < {_fmt(scenario.min_artifact)}: the negative control "
                "(a pre-0.4.3 hang image never rolls back); build the hang image at HEAD",
            )

    # Forbidden rows: a FAIL whether or not the transaction has closed.
    seen = [s for s in scenario.forbidden if s in states]
    if scenario.forbidden:
        add(
            "forbidden-rows",
            not seen,
            f"none of {', '.join(scenario.forbidden)}"
            if not seen
            else f"has {', '.join(seen)}, which this step must never produce",
        )

    # power-cut: the open end is the pass (deploys.py rule 1). Never INCOMPLETE.
    if scenario.open_end:
        ok = (not last.terminal) and last.state in scenario.open_end
        add(
            "open-end",
            ok,
            f"last row {last.state!r} is open, as a cut mid-download leaves it"
            if ok
            else f"last row {last.state!r}{' [terminal]' if last.terminal else ''}; "
            f"want an open {' or '.join(scenario.open_end)}",
        )
        return _verdict(checks, incomplete=False), checks

    if not last.terminal:
        checks.append(
            Check(
                "terminal",
                "PENDING",
                f"last row {last.state!r} is not terminal; run the judge again later",
            )
        )
        return _verdict(checks, incomplete=True), checks

    # Terminal shape.
    allowed = [t for t in scenario.terminal if t[0] == last.state]
    if not allowed:
        want = " or ".join(t[0] for t in scenario.terminal)
        why = f"terminal {last.state!r} ({last.detail or 'no detail'}); want {want}"
        if last.state == "failed":
            why += " — unexpected failure, record it"
        add("terminal", False, why)
    else:
        details = allowed[0][1]
        if details is None:
            add("terminal", True, f"terminal {last.state!r}")
        else:
            ok = last.detail in details
            add(
                "terminal",
                ok,
                f"terminal {last.state!r} with {last.detail!r}"
                if ok
                else f"terminal {last.state!r} with {last.detail!r}: unexpected failure,"
                f" record it (want {' or '.join(details)})",
            )

    order = scenario.order
    if last.state == "confirmed" and not order and "confirmed" in dict(scenario.terminal):
        order = ("staged", "confirmed")
    if order:
        ok = is_subsequence(order, states)
        add(
            "order",
            ok,
            " -> ".join(order) if ok else f"rows do not contain {' -> '.join(order)} in order",
        )

    if scenario.rollback_detail:
        ok = bool(ROLLBACK_DETAIL_RE.match(last.detail))
        add(
            "rollback-detail",
            ok,
            f"{last.detail!r}"
            if ok
            else f"{last.detail!r} is not the agent's `returned to ota_N; ota_M did not confirm`",
        )

    for state in scenario.best_effort:
        if state not in states:
            add(state, False, f"best-effort row missing ({state})", soft=True)

    if allowed and (scenario.window_min is not None or scenario.window_max is not None):
        checks.append(_check_window(scenario, rows, last))

    return _verdict(checks, incomplete=False), checks


def _check_window(scenario: Scenario, rows: list[Row], last: Row) -> Check:
    """Seconds from t_ref (the `rebooting` row, else `staged`) to the terminal row."""
    rebooting = _last_at(rows, "rebooting")
    if scenario.window_needs_rebooting and rebooting is None:
        return Check(
            "window",
            "SKIP",
            "no `rebooting` row: a `staged` reference includes operator delay; bound skipped",
        )
    ref_name = "rebooting" if rebooting is not None else "staged"
    t_ref = rebooting if rebooting is not None else _last_at(rows, "staged")
    if t_ref is None:
        return Check("window", FAIL, "no `rebooting` or `staged` row to time from")
    gap = last.at - t_ref
    span = f"{ref_name} -> {last.state} = {gap:.1f} s"
    if scenario.window_min is not None and gap < scenario.window_min:
        return Check("window", FAIL, f"{span}, under {scenario.window_min:.0f} s")
    if scenario.window_max is not None and gap > scenario.window_max:
        result = WARN if scenario.window_max_warn_only else FAIL
        return Check("window", result, f"{span}, over {scenario.window_max:.0f} s")
    return Check("window", PASS, span)


def _verdict(checks: list[Check], *, incomplete: bool) -> str:
    if any(c.result == FAIL for c in checks):
        return "FAIL"
    return "INCOMPLETE" if incomplete else "PASS"


def render(scenario: Scenario, rows: list[Row], verdict: str, checks: list[Check]) -> str:
    """The rows, the checks and the last line `JUDGE <verdict> <scenario>`."""
    first = rows[0]
    head = first if first.state == "requested" else rows[-1]
    lines = [
        f"cmd {first.cmd_id[:8]}…  {head.from_version or '?'} -> {head.artifact_version or '?'}"
        f"  ({first.cmd_id})"
    ]
    t0 = first.at
    for r in rows:
        mark = "[t]" if r.terminal else "[ ]"
        detail = f"  {r.detail}" if r.detail else ""
        lines.append(f"  +{r.at - t0:7.1f}s  {r.state:<13} {mark}{detail}")
    for c in checks:
        lines.append(f"check: {c.name} ... {c.result} — {c.why}")
    for note in scenario.notes:
        lines.append(f"note: {note}")
    lines.append(f"JUDGE {verdict} {scenario.name}")
    return "\n".join(lines)


EXIT_FOR = {"PASS": EXIT_PASS, "FAIL": EXIT_FAIL, "INCOMPLETE": EXIT_INCOMPLETE}


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        description="Grade one deploy transaction against a bench-replay scenario (R2b-test-2)."
    )
    parser.add_argument("scenario", nargs="?", help="scenario name (see --list)")
    parser.add_argument("--sql", metavar="REF", help="print the SELECT for REF and exit")
    parser.add_argument("--list", action="store_true", help="list the scenarios")
    args = parser.parse_args(argv)

    if args.list:
        for s in SCENARIOS.values():
            print(f"{s.name:<19} {s.step}")
        return EXIT_PASS
    if args.sql is not None:
        try:
            print(build_sql(args.sql))
        except RefError as exc:
            print(exc, file=sys.stderr)
            return EXIT_USAGE
        return EXIT_PASS
    if args.scenario is None:
        parser.print_usage(sys.stderr)
        return EXIT_USAGE
    scenario = SCENARIOS.get(args.scenario)
    if scenario is None:
        print(
            f"unknown scenario {args.scenario!r}; one of: {', '.join(SCENARIOS)}",
            file=sys.stderr,
        )
        return EXIT_USAGE

    try:
        rows = parse_rows(sys.stdin.read())
    except ValueError as exc:
        print(exc, file=sys.stderr)
        return EXIT_USAGE
    if not rows:
        print("no rows for REF: no such transaction (check the device id / cmd_id and the stack)")
        return EXIT_USAGE
    verdict, checks = judge(scenario, rows)
    print(render(scenario, rows, verdict, checks))
    return EXIT_FOR[verdict]


if __name__ == "__main__":
    sys.exit(main())

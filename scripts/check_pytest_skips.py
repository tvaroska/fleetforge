#!/usr/bin/env python3
"""Enforce the Fleetforge verification gate's pytest skip allowlist.

Fails the gate when:
* `FF_ALLOW_DB_SKIP=1` is set under `CI`;
* PostgreSQL was unreachable / skipped without an explicit local `FF_ALLOW_DB_SKIP=1`;
* any skipped test falls outside the narrow non-DB allowlist (absent prebuilt agent
  bundles, or opt-in MinIO suites when `FF_RUN_MINIO_TESTS != 1`);
* the `-rs` skipped line tally disagrees with pytest's final skipped summary count.
"""

from __future__ import annotations

import os
import re
import sys
from collections.abc import Mapping
from pathlib import Path

SKIPPED_LINE_RE = re.compile(r"^SKIPPED\s+(?:\[(\d+)\]\s+)?(.+)$")
SUMMARY_SKIPPED_RE = re.compile(r"\b(\d+)\s+skipped\b")
FORBIDDEN_DB_SNIPPETS = (
    "Cannot reach PostgreSQL",
    "PostgreSQL unreachable",
)
MAX_BUNDLE_SKIPS = 2
MAX_MINIO_SKIPS = 10


def is_ci_env(env: Mapping[str, str]) -> bool:
    """Return True when `CI` is set to a truthy value."""
    return env.get("CI", "").strip().lower() not in ("", "0", "false")


def _extract_summary_skipped(output: str) -> int:
    for line in reversed(output.splitlines()):
        stripped = line.strip()
        if not stripped or " in " not in stripped:
            continue
        if any(tok in stripped for tok in ("passed", "skipped", "failed", "error", "xfailed")):
            match = SUMMARY_SKIPPED_RE.search(stripped)
            return int(match.group(1)) if match else 0
    return 0


def validate_pytest_output(output: str, env: Mapping[str, str]) -> list[str]:
    """Return a list of violation messages (empty list means pass)."""
    allow_db_skip = env.get("FF_ALLOW_DB_SKIP") == "1"
    if allow_db_skip and is_ci_env(env):
        return ["FF_ALLOW_DB_SKIP=1 is forbidden when CI is set"]
    if allow_db_skip:
        return []

    errors: list[str] = []
    for snippet in FORBIDDEN_DB_SNIPPETS:
        if snippet in output:
            errors.append(
                f"database-backed tests did not run ({snippet!r} observed in pytest output)"
            )

    run_minio = env.get("FF_RUN_MINIO_TESTS") == "1"
    bundle_skips = 0
    minio_skips = 0
    parsed_total = 0

    for line in output.splitlines():
        match = SKIPPED_LINE_RE.match(line.strip())
        if not match:
            continue
        count = int(match.group(1) or "1")
        detail = match.group(2)
        parsed_total += count

        if "tests/test_agent_power_and_size.py" in detail and "no bundle in agent/dist" in detail:
            bundle_skips += count
        elif (
            not run_minio
            and (
                "tests/test_artifact_download_minio.py" in detail
                or "tests/test_object_store_minio.py" in detail
            )
            and "MinIO tests opt-in via FF_RUN_MINIO_TESTS=1" in detail
        ):
            minio_skips += count
        else:
            errors.append(f"unallowlisted pytest skip: {line.strip()}")

    if bundle_skips > MAX_BUNDLE_SKIPS:
        errors.append(f"bundle skips ({bundle_skips}) exceeded budget ({MAX_BUNDLE_SKIPS})")
    if minio_skips > MAX_MINIO_SKIPS:
        errors.append(f"MinIO skips ({minio_skips}) exceeded budget ({MAX_MINIO_SKIPS})")

    summary_skipped = _extract_summary_skipped(output)
    if summary_skipped != parsed_total:
        errors.append(
            f"pytest reported {summary_skipped} skipped tests, "
            f"but -rs lines accounted for {parsed_total} (ensure pytest runs with -rs)"
        )
    max_total = MAX_BUNDLE_SKIPS if run_minio else (MAX_BUNDLE_SKIPS + MAX_MINIO_SKIPS)
    if summary_skipped > max_total:
        errors.append(f"total skipped tests ({summary_skipped}) exceeded budget ({max_total})")

    return errors


def main(argv: list[str] | None = None) -> int:
    args = sys.argv[1:] if argv is None else argv
    if len(args) != 1:
        print("usage: check_pytest_skips.py <pytest-output-file>", file=sys.stderr)
        return 2
    output = Path(args[0]).read_text(encoding="utf-8", errors="replace")
    errors = validate_pytest_output(output, os.environ)
    if errors:
        for err in errors:
            print(f"ERROR: {err}", file=sys.stderr)
        return 1
    if os.environ.get("FF_ALLOW_DB_SKIP") == "1":
        print(
            "WARNING: FF_ALLOW_DB_SKIP=1 enabled locally — DB skip guard bypassed.",
            file=sys.stderr,
        )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

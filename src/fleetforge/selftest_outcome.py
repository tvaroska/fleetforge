"""The custom self-test's outcome, read out of a deploy's stored `detail`. R5-be-1.

The agent (R5-fw-2) decides confirm or rollback on the maker's self-test and reports
the result only as `up/status.detail` text (R5-spec-1 D11). This module is the **one
reader** of that grammar, the way `TERMINAL_DEPLOY_STATES` is the one copy of the
terminal set: the API, the SSE frame and the tests all go through it, so no client
keeps a second copy of the regex.

Named `selftest_outcome`, not `selftest`: `python -m fleetforge.broker selftest` and
`fleetforge.storage selftest` already use that word for infrastructure probes.

Parsed on read, never stored: no column, no JSONB key, no migration, and rows that
exist already parse with no backfill. Transport-agnostic like `deploys.py`: no FastAPI,
no SQLAlchemy, no settings.

**The grammar counts only on the state it belongs to.** `passed` only on `confirmed`
(and with no `; returned to` tail), `failed` / `timed out` only on `rolled_back`. The
firmware writes them nowhere else, so a contradictory pair is a forgery or a bug, and
showing "passed" next to a rollback would be a lie. A detail that is not the grammar
(`the bootloader never armed rollback for this image`, `rolled back by command; ...`,
the legacy `returned to ota_0; ota_1 did not confirm`, the safe-window waits) is simply
not a self-test outcome: `None`.

The KPIs do not change: a self-test rollback is a `rolled_back`, a *save* for fleet
safety and a *miss* for delivery success. There is no `selftest_failed` deploy state.
"""

import re
from dataclasses import dataclass
from typing import Literal

from fleetforge.db.models import DeployState

# R5-spec-1 D11's `^selftest (passed|failed: [^;]*|timed out)(; returned to \S+)?$`,
# used with `.fullmatch()`: Python's `$` also matches before a trailing newline, and
# `fullmatch` does not.
SELFTEST_DETAIL_RE = re.compile(r"selftest (passed|failed: [^;]*|timed out)(; returned to (\S+))?")

SelfTestResult = Literal["passed", "failed", "timed_out"]

_FAILED_PREFIX = "failed: "


@dataclass(frozen=True, slots=True)
class SelfTestOutcome:
    """What the device's self-test said.

    `reason` is only for `failed`: the text after `selftest failed: `, exactly as stored
    (already sanitised), or None when empty. `returned_to` is the slot label after
    `; returned to `, else None. Both are device-controlled text: render them as text.
    """

    outcome: SelfTestResult
    reason: str | None
    returned_to: str | None


def parse_selftest_detail(detail: str | None) -> SelfTestOutcome | None:
    """The grammar alone: None for None, "" or anything that does not fully match."""
    if not detail:
        return None
    match = SELFTEST_DETAIL_RE.fullmatch(detail)
    if match is None:
        return None
    head = match.group(1)
    returned_to = match.group(3)
    if head == "passed":
        return SelfTestOutcome("passed", None, returned_to)
    if head == "timed out":
        return SelfTestOutcome("timed_out", None, returned_to)
    return SelfTestOutcome("failed", head.removeprefix(_FAILED_PREFIX) or None, returned_to)


def selftest_outcome(state: str, detail: str | None) -> SelfTestOutcome | None:
    """The grammar plus the state-consistency rule (module docstring)."""
    parsed = parse_selftest_detail(detail)
    if parsed is None:
        return None
    if parsed.outcome == "passed":
        if state == DeployState.CONFIRMED.value and parsed.returned_to is None:
            return parsed
        return None
    if state == DeployState.ROLLED_BACK.value:
        return parsed
    return None

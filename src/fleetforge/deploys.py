"""`deploy_events` — the single writer, and the reads beside it. R1-be-2, R1-be-4, R1-fe-1.

`spec/prd.md` → *Retention*: this table is kept **forever**; "the metric history is the
product's evidence". Both v1 KPIs (delivery success, fleet safety) are computed over the
terminal event of each `(device_id, cmd_id)` transaction, so a row written in the wrong
shape is not a bug that shows up today — it is a KPI that is quietly wrong in R5.

**Everything that inserts into `deploy_events` goes through this module.** The
ingestor's `up/status` writer lives *here* (`record_observed_status`), rather than
growing its own SQL in `ingestor/handlers.py`; `tests/test_invariants.py` holds the
tripwire that no other module under `src/` mentions the table in SQL. One writer is
what keeps "what does `is_terminal` mean" a single answer.

Transport-agnostic like `fleetforge.progress` and `fleetforge.presence`: no FastAPI
import and no settings object — the caller passes the session and the numbers.

## The three rules this module owns

**1. The server authors exactly one state, and one exception to it.**

`requested` is server-authored and never arrives on the wire: it records *"we published
a command"*, which no device can report. Without it, an abandoned deploy is invisible to
the KPIs and R1-be-4 cannot map an incoming `cmd_id` back to the version that was
intended.

The exception is `failed` with `detail={"reason": "publish_failed"}`. It is the one
**terminal** state the server may write, and only when the publish itself failed — a
server-side outcome of a command the device provably never saw. Beyond that:

> *The server never writes a state for a transaction the device did receive, and never
> expires `awaiting_safe_window`.*

`awaiting_safe_window` may last forever — a vehicle in motion, a drone in the air
(`spec/device-protocol.md`; `design/architecture.md` principle 5, the device owns the
reboot). There is no sweeper, no timeout task and no `asyncio.sleep` in this module or
in `api/routers/deploys.py`, and `tests/test_api_deploy.py` asserts their absence.

**2. One `cmd_id` per deploy *intent*, reused across retries.**

The device deduplicates on the command `id` (`spec/device-protocol.md`), so a retried
`stage` must carry the id the first attempt used or the board downloads the same
firmware twice. `latest_open_transaction` implements the window:

> A POST for `(device_id, sha256)` matching the most recent `requested` row for that
> device, where that row is younger than the signed URL's TTL and its
> `(device_id, cmd_id)` transaction has no terminal event, reuses that row's `cmd_id`
> — a fresh URL is signed and the command republished, and **no second row is
> written**. Anything else is a new intent and mints a new `cmd_id`.

A *different* artifact is always a new transaction. The TTL bound is deliberate: past
it the first URL has expired, so a board that never acted on the first command cannot
act on it now, and a fresh intent is the honest record.

**3. A device-reported state is recorded at most once per `(device_id, cmd_id, state)`.**

`up/status` is **retained** (`spec/device-protocol.md`) and the ingestor re-`subscribe`s
on every connect, so every broker blip, container restart or stack deploy replays the
last status of every board. Ingesting the replay is mandatory — it is how an outcome
published while the ingestor was down is delivered at all — so the deduplication has to
live in the writer: `record_observed_status` returns `None` when the triple is already
on record.

The consequence, deliberate: a genuinely repeated state inside one transaction (a
`downloading → failed → downloading` retry) collapses to its first occurrence, and the
`pct` stored is the first one seen. This table is a log of **transitions**, not a
progress feed — live progress is the SSE stream's job. For the same reason there is no
unique index: a repeated state is legal data, so this is a writer rule and not a
database invariant.

**Nothing secret goes in `detail`.** The signed URL is a bearer credential: it is never
persisted here, never logged and never echoed in an API response. Device-reported
detail is control-stripped, truncated and URL-redacted before it is stored, because it
is device-controlled text landing in a table kept forever.

**The reads live here too** (`latest_open_transaction`, `latest_deploys`). The
single-writer tripwire in `tests/test_invariants.py` bans other modules from writing
this table; the *spirit* extends to reading it, for the same reason `progress.py` holds
`latest_progress` next to `record_progress` and `api/routers/devices.py` merely calls
it. SQL about `deploy_events` belongs to this module, not to a router.
"""

import datetime as dt
import logging
import re
from collections.abc import Sequence
from dataclasses import dataclass
from typing import Any

from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncSession

from fleetforge.db.models import TERMINAL_DEPLOY_STATES, DeployEvent, DeployState

logger = logging.getLogger(__name__)

# Same bound as `api/schemas.py::MAX_PROGRESS_DETAIL`, restated rather than imported:
# this module is transport-agnostic and must not reach into the FastAPI side.
MAX_OBSERVED_DETAIL = 200

# The signed download URL is a bearer credential. Our agent does not echo it, but a
# third-party one might, and `deploy_events` is kept forever — so redact by
# construction rather than trusting the fleet.
_URL_RE = re.compile(r"https?://\S+")

# The newest `requested` row for this device, inside the reuse window, whose
# transaction has no terminal event. `NOT EXISTS` rather than a join so a transaction
# with many observed states still yields one row, and the index
# `ix_deploy_events_device_id_at` carries the ordering.
_OPEN_TRANSACTION_SQL = text(
    """
    SELECT cmd_id, artifact_version, detail
      FROM deploy_events AS requested
     WHERE requested.device_id = :device_id
       AND requested.state = :requested_state
       AND requested.cmd_id IS NOT NULL
       AND requested.at > now() - make_interval(secs => :window_s)
       AND NOT EXISTS (
           SELECT 1
             FROM deploy_events AS terminal
            WHERE terminal.device_id = requested.device_id
              AND terminal.cmd_id = requested.cmd_id
              AND terminal.is_terminal
       )
     ORDER BY requested.at DESC, requested.id DESC
     LIMIT 1
    """
)


@dataclass(frozen=True, slots=True)
class OpenTransaction:
    """An in-flight deploy intent a repeat request may re-use.

    `sha256` comes out of the stored `detail`, which is why `record_requested` puts it
    there: the artifact identity is what makes "the same deploy" the same deploy, and a
    version label can be re-pointed at other bytes only by being deleted first.
    """

    cmd_id: str
    artifact_version: str | None
    sha256: str | None


async def latest_open_transaction(
    session: AsyncSession, *, device_id: str, window_s: int
) -> OpenTransaction | None:
    """The newest reusable `requested` transaction for `device_id`, or None.

    "Reusable" is the rule in the module docstring: `requested`, younger than
    `window_s` (the signed-URL TTL), and with no terminal event. The caller still has
    to compare the artifact — a different artifact is a different intent, and this
    function deliberately does not take a `sha256` so the *decision* stays in one
    readable place in `api/routers/deploys.py`.
    """
    row = (
        await session.execute(
            _OPEN_TRANSACTION_SQL,
            {
                "device_id": device_id,
                "requested_state": DeployState.REQUESTED.value,
                "window_s": window_s,
            },
        )
    ).first()
    if row is None:
        return None
    detail = row.detail if isinstance(row.detail, dict) else {}
    sha256 = detail.get("sha256")
    return OpenTransaction(
        cmd_id=row.cmd_id,
        artifact_version=row.artifact_version,
        sha256=sha256 if isinstance(sha256, str) else None,
    )


async def record_requested(
    session: AsyncSession,
    *,
    device_id: str,
    cmd_id: str,
    artifact_version: str,
    from_version: str | None,
    sha256: str,
    size_bytes: int,
    target: str,
    apply: str,
) -> DeployEvent:
    """Record the intent to deploy: one `requested` row, never terminal.

    Called **before** the publish and committed before it (`api/routers/deploys.py`):
    record the intent, then act — the same posture as enrolment's "commit, then
    provision". A crash between the two leaves an honest "requested, never observed"
    transaction; the reverse order would leave a board downloading firmware that
    nothing in the history mentions.

    `detail` carries what the KPI query and the operator need to understand the row
    without joining three tables — and nothing else. **No URL, no bucket, no key.**
    """
    row = DeployEvent(
        device_id=device_id,
        cmd_id=cmd_id,
        state=DeployState.REQUESTED.value,
        is_terminal=False,
        from_version=from_version,
        artifact_version=artifact_version,
        detail={
            "sha256": sha256,
            "size_bytes": size_bytes,
            "target": target,
            "apply": apply,
        },
    )
    session.add(row)
    await session.flush()
    return row


async def record_publish_failure(
    session: AsyncSession,
    *,
    device_id: str,
    cmd_id: str,
    artifact_version: str,
    from_version: str | None,
) -> DeployEvent:
    """The one terminal state the server may author: the command was never published.

    `failed` / `is_terminal=True` / `detail={"reason": "publish_failed"}`. It is honest
    because the broker refused or was unreachable, so the device provably never saw the
    command — it closes the transaction the `requested` row opened rather than leaving
    it open forever, and the API answers 503.

    Do not extend this function to author any other terminal state. A device that *did*
    receive a command owns its own outcome, including sitting in `awaiting_safe_window`
    indefinitely.
    """
    row = DeployEvent(
        device_id=device_id,
        cmd_id=cmd_id,
        state=DeployState.FAILED.value,
        # From the set, not a literal `True`: the terminal vocabulary lives in
        # `db/models.py` and this row must agree with the KPI queries' definition.
        is_terminal=DeployState.FAILED in TERMINAL_DEPLOY_STATES,
        from_version=from_version,
        artifact_version=artifact_version,
        detail={"reason": "publish_failed"},
    )
    session.add(row)
    await session.flush()
    logger.warning(
        "deploy %s for %s recorded as failed: the command was not published", cmd_id, device_id
    )
    return row


# Has this exact transition already been recorded? Covered by `ix_deploy_events_cmd_id`.
# Safe without a unique index because the ingestor is the only MQTT subscriber and
# processes messages sequentially, one session per message (`design/production.md`).
_ALREADY_RECORDED_SQL = text(
    """
    SELECT 1
      FROM deploy_events
     WHERE device_id = :device_id
       AND cmd_id = :cmd_id
       AND state = :state
     LIMIT 1
    """
)

# The intent this observed state belongs to. Copying the two version columns off the
# `requested` row is the whole reason R1-be-2 invented that state: without it a
# terminal row cannot say which version was intended and "delivery success" is
# uncomputable.
_INTENT_SQL = text(
    """
    SELECT artifact_version, from_version
      FROM deploy_events
     WHERE device_id = :device_id
       AND cmd_id = :cmd_id
       AND state = :requested_state
     ORDER BY at DESC, id DESC
     LIMIT 1
    """
)


def _sanitise_detail(value: str) -> str:
    """Make device-controlled text safe to keep forever.

    Three rules, each with a precedent: no control characters (a device that can inject
    a newline can forge a log line — `api/schemas.py::_printable_detail`), no URLs (the
    signed link is a credential), and bounded length (`MAX_PROGRESS_DETAIL`).
    Redaction happens before truncation so a URL cannot be half-kept.
    """
    redacted = _URL_RE.sub("<url>", value)
    stripped = "".join(ch for ch in redacted if ch >= " " and ch != "\x7f")
    return stripped[:MAX_OBSERVED_DETAIL]


def _observed_detail(pct: int | None, detail: str | None) -> dict[str, Any] | None:
    """The JSONB blob for an observed state: what the device sent, and nothing else.

    The key names are the wire's own (`spec/device-protocol.md` → `up/status`), so a
    reader of the protocol can read the column. `None` when the device said neither.
    """
    blob: dict[str, Any] = {}
    if pct is not None:
        blob["pct"] = pct
    if detail is not None:
        text_detail = _sanitise_detail(detail)
        if text_detail:
            blob["detail"] = text_detail
    return blob or None


async def record_observed_status(
    session: AsyncSession,
    *,
    device_id: str,
    cmd_id: str,
    state: str,
    at: dt.datetime,
    pct: int | None = None,
    detail: str | None = None,
) -> DeployEvent | None:
    """Record a state the **device** reported on `up/status`, or `None` if nothing was.

    Every outcome a board reports becomes a row — the successes, the failures and the
    abandoned ones — because both v1 KPIs are computed over the terminal event of each
    transaction. Three reasons this returns `None` instead of writing:

    * the wire claimed `requested`, which is the server's own state and no device's to
      report (`db/models.py`): a firmware bug or a forgery, logged at WARNING;
    * the `(device_id, cmd_id, state)` transition is already on record — the retained
      replay rule in the module docstring;
    * (the caller's business) the status carried no `cmd_id`, so it belongs to no
      transaction and there is nothing to record.

    A `cmd_id` with no `requested` row — a board replaying a transaction from before a
    database rebuild, a command issued by another server — is still recorded, with both
    version columns NULL. "Every outcome" means every outcome, including the ones we
    cannot explain.

    `at` is the server's receipt time, threaded in by the caller and never the device's
    `ts` (`spec/device-protocol.md` → *Clock*): a reconnect can drain a backlog long
    after the board wrote it.
    """
    if state == DeployState.REQUESTED.value:
        logger.warning(
            "device %s reported the server-authored state %r for %s; ignoring",
            device_id,
            state,
            cmd_id,
        )
        return None

    keys = {"device_id": device_id, "cmd_id": cmd_id, "state": state}
    if (await session.execute(_ALREADY_RECORDED_SQL, keys)).first() is not None:
        logger.debug("deploy %s for %s already recorded state %s", cmd_id, device_id, state)
        return None

    intent = (
        await session.execute(
            _INTENT_SQL,
            {
                "device_id": device_id,
                "cmd_id": cmd_id,
                "requested_state": DeployState.REQUESTED.value,
            },
        )
    ).first()
    if intent is None:
        logger.info(
            "recording state %s for %s with no requested row for cmd %s", state, device_id, cmd_id
        )

    row = DeployEvent(
        device_id=device_id,
        cmd_id=cmd_id,
        state=state,
        # From the set, never a literal: the terminal vocabulary lives in `db/models.py`
        # and this row has to agree with the KPI queries' definition. A state nobody has
        # heard of is recorded and is not terminal.
        is_terminal=state in TERMINAL_DEPLOY_STATES,
        # The server's receipt time, not the column default: a reconnect can drain a
        # backlog of retained statuses long after the message was received.
        at=at,
        from_version=intent.from_version if intent is not None else None,
        artifact_version=intent.artifact_version if intent is not None else None,
        detail=_observed_detail(pct, detail),
    )
    session.add(row)
    await session.flush()
    if row.is_terminal:
        # The ops line that answers "did it land?". Never the `detail` text.
        logger.info("deploy %s for %s ended: %s", cmd_id, device_id, state)
    return row


# The newest row per device, which *is* the newest transaction's current state: rows are
# appended in receipt order and never updated. `DISTINCT ON` reads straight down
# `ix_deploy_events_device_id_at`, the same reason `progress.LATEST_SQL` is hand-written.
#
# `id DESC` is load-bearing, not tidiness: `record_requested` and the board's first
# `up/status` routinely land inside one millisecond, and on `at DESC` alone the
# `requested` row can sort above the `staging` that followed it — the dashboard would
# then walk backwards. There is deliberately **no time window**: a terminal row from last
# week is the honest answer to "what was the last thing that happened to this board", and
# the caller renders it muted.
_LATEST_DEPLOYS_SQL = text(
    """
    SELECT DISTINCT ON (device_id)
           device_id, cmd_id, state, at, is_terminal, artifact_version, from_version, detail
      FROM deploy_events
     WHERE device_id = ANY(:device_ids)
     ORDER BY device_id, at DESC, id DESC
    """
)


@dataclass(frozen=True, slots=True)
class DeploySnapshot:
    """One board's newest deploy state — the read model behind the dashboard's cell.

    A plain dataclass rather than a `DeployEvent`, the `progress.Arrival` posture: the
    caller wants the answer, and handing back the ORM row would invite a second place
    to decide what `pct` means or to surface a column this is not about.

    `state` is **device-controlled text** with an advisory vocabulary (`DeployState` is
    a `StrEnum` over a TEXT column with no CHECK), so a renderer looks it up with a
    fallback and never switches on it exhaustively. `is_terminal` is the server's
    answer, sourced from `TERMINAL_DEPLOY_STATES` at write time — never re-derived
    downstream from a second copy of that vocabulary.

    `pct` is a transition's progress reading, not a progress feed: the writer keeps the
    first `pct` it saw for a repeated state (rule 3 above), so a client that animated it
    would sit at one number through a whole download.
    """

    cmd_id: str | None
    state: str
    at: dt.datetime
    is_terminal: bool
    artifact_version: str | None
    from_version: str | None
    pct: int | None
    detail: str | None


def _snapshot_pct(detail: Any) -> int | None:
    """`pct` out of the JSONB blob, and only when it really is an `int`.

    `bool` is an `int` in Python and JSONB round-trips `true` as one, so it is excluded
    explicitly — the `isinstance` posture `latest_open_transaction` takes with `sha256`.
    """
    if not isinstance(detail, dict):
        return None
    value = detail.get("pct")
    return value if isinstance(value, int) and not isinstance(value, bool) else None


def _snapshot_detail(detail: Any) -> str | None:
    """`detail` out of the JSONB blob — that key and no other.

    A `requested` row's blob carries `sha256`/`size_bytes`/`target`/`apply`, which is
    not a lie but is not what `detail` means in the API, so nothing else is surfaced.
    The stored string was already sanitised at write time (`_sanitise_detail`): do not
    re-sanitise it here, and above all do not un-redact it.
    """
    if not isinstance(detail, dict):
        return None
    value = detail.get("detail")
    return value if isinstance(value, str) else None


async def latest_deploys(
    session: AsyncSession, *, device_ids: Sequence[str]
) -> dict[str, DeploySnapshot]:
    """The newest deploy state of each of `device_ids`, keyed by device.

    Boards with no `deploy_events` row are simply absent from the mapping — "never
    deployed to" is a different fact from "deployed and we do not know how it went",
    and the caller renders them differently.

    `device_ids` comes from the caller rather than being a full-table scan, so the read
    is bounded by whatever cap the caller's own list carries (`devices.LIST_LIMIT`) and
    an empty list costs one trivially-false query rather than a special case.
    """
    rows = (await session.execute(_LATEST_DEPLOYS_SQL, {"device_ids": list(device_ids)})).all()
    return {
        row.device_id: DeploySnapshot(
            cmd_id=row.cmd_id,
            state=row.state,
            at=row.at,
            is_terminal=row.is_terminal,
            artifact_version=row.artifact_version,
            from_version=row.from_version,
            pct=_snapshot_pct(row.detail),
            detail=_snapshot_detail(row.detail),
        )
        for row in rows
    }

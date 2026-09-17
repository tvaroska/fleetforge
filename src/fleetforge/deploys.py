"""`deploy_events` — the single writer. R1-be-2.

`spec/prd.md` → *Retention*: this table is kept **forever**; "the metric history is the
product's evidence". Both v1 KPIs (delivery success, fleet safety) are computed over the
terminal event of each `(device_id, cmd_id)` transaction, so a row written in the wrong
shape is not a bug that shows up today — it is a KPI that is quietly wrong in R5.

**Everything that inserts into `deploy_events` goes through this module.** R1-be-4's
ingestor path adds its `up/status` writer *here*, rather than growing its own SQL in
`ingestor/handlers.py`; `tests/test_invariants.py` holds the tripwire that no other
module under `src/` mentions the table in SQL. One writer is what keeps "what does
`is_terminal` mean" a single answer.

Transport-agnostic like `fleetforge.progress` and `fleetforge.presence`: no FastAPI
import and no settings object — the caller passes the session and the numbers.

## The two rules this module owns

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

**Nothing secret goes in `detail`.** The signed URL is a bearer credential: it is never
persisted here, never logged and never echoed in an API response.
"""

import logging
from dataclasses import dataclass

from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncSession

from fleetforge.db.models import TERMINAL_DEPLOY_STATES, DeployEvent, DeployState

logger = logging.getLogger(__name__)

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

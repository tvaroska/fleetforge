"""The enrollment credential's lifecycle: issue, revoke, burn. **CRITICAL.**

`CRITICAL.md`: "A token that fails to burn lets anyone with one board enrol arbitrary
devices into the fleet." The broker and the API are on the public internet from R0
(`spec/prd.md` → *Deployment model*), so there is no LAN perimeter behind which a
leaked or re-usable token would be harmless.

This module is transport-agnostic, like the rest of `fleetforge.auth`: it holds no
FastAPI or Starlette import and no request/response types. It does touch the ORM
model and SQLAlchemy Core `text()`, because the burn *is* a SQL statement and the
whole point of shipping it here is that there is exactly one copy of it.

Two readers, one predicate. `BURN_SQL` is the authorization decision (R0-be-4);
`token_status()` is the display value the dashboard renders. They are defined
against the same rule on purpose — see `token_status`.
"""

import datetime as dt
import uuid
from enum import StrEnum

from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncSession

from fleetforge.auth.tokens import ENROLLMENT_TOKEN_PREFIX, aissue_token
from fleetforge.db.models import EnrollmentToken

# The burn. One conditional UPDATE, and the only copy of it in the codebase.
#
# (a) Zero rows back means the token was already burned, revoked or expired —
#     reject the enrollment. There is no other way to read a zero-row result.
# (b) Correct under PostgreSQL's default READ COMMITTED: the loser of a concurrent
#     race blocks on the winner's row lock, re-evaluates the predicate after the
#     winner commits, and correctly affects zero rows. Proven by the two-connection
#     race in `tests/test_invariants.py`.
# (c) NEVER implement this as SELECT → check → UPDATE. That is the TOCTOU that hands
#     over the fleet: two devices read "unused" and both enroll.
# (d) If a connection is ever REPEATABLE READ or SERIALIZABLE, handle serialization
#     failures (SQLSTATE 40001) instead of assuming success — under those levels the
#     loser raises rather than returning zero rows.
# (e) `R0-be-4` imports this constant. It does not write its own, and it does not
#     copy this text into a handler.
BURN_SQL = text(
    """
    UPDATE enrollment_tokens
       SET used_at = now(), used_by_device_id = :device_id
     WHERE id = :token_id
       AND used_at IS NULL
       AND revoked_at IS NULL
       AND expires_at > now()
    RETURNING id
    """
)

# The grace-window lookup that recovers a LOST ENROLLMENT RESPONSE (R0-be-4 §2.4).
#
# NOT a second burn, and NOT a weakening of single use: it can only ever match the
# device the token was ALREADY burned for (`used_by_device_id = :device_id`), so a
# token still enrolls exactly one board, forever. What it buys is the case the wire
# cannot avoid — the agent writes its broker credential to NVS only *after* it reads
# the response body, so a dropped packet on a first boot over flaky Wi-Fi otherwise
# leaves a board that is enrolled server-side, has no credential, and holds a token
# that can never burn again. That is a re-flash, in the field.
#
# `FOR UPDATE` matters: it takes the same row lock the burn would, so a concurrent
# `POST …/revoke` either lands before this and disqualifies the row (`revoked_at IS
# NULL` fails), or blocks until the enrollment commits. Without it this is a TOCTOU
# against revocation.
#
# The window is `config.enroll_retry_window_s` (600 s), passed in — one number, one
# place, and the database clock stays the authority for both statements.
RETRY_LOOKUP_SQL = text(
    """
    SELECT id
      FROM enrollment_tokens
     WHERE id = :token_id
       AND used_by_device_id = :device_id
       AND revoked_at IS NULL
       AND expires_at > now()
       AND used_at > now() - make_interval(secs => :window_s)
       FOR UPDATE
    """
)

# Conditional for the same reason `admin_tokens`' revoke is (`api/routers/auth.py`):
# a second revoke must be a no-op, not a moved timestamp. `RETURNING id` lets the
# caller tell "already revoked" (zero rows) from "unknown id" without a pre-read.
REVOKE_SQL = text(
    "UPDATE enrollment_tokens SET revoked_at = now() "
    "WHERE id = :token_id AND revoked_at IS NULL RETURNING id"
)


class EnrollmentTokenStatus(StrEnum):
    """The four states an enrollment token can be observed in."""

    ACTIVE = "active"
    USED = "used"
    REVOKED = "revoked"
    EXPIRED = "expired"


def token_status(row: EnrollmentToken, now: dt.datetime) -> EnrollmentTokenStatus:
    """Derive the operator-facing status of `row` as of `now`.

    **The invariant:** the result is `ACTIVE` **iff** `BURN_SQL` would affect that
    row. A dashboard that says "active" for a token the burn rejects (or the reverse)
    sends someone to the bench with a board that will not enroll;
    `tests/test_enrollment_tokens.py::test_status_matches_burn_predicate` asserts the
    equivalence over all four states so the two rules cannot drift.

    Precedence, in this order:

    1. `used_at` set → `USED`. This is the outcome an operator cares about first, and
       `used_by_device_id` says which board took the token.
    2. `revoked_at` set → `REVOKED`.
    3. `expires_at <= now` → `EXPIRED`. Note the strict `>` in `BURN_SQL`: a token
       whose deadline is exactly `now` is dead in both places.
    4. otherwise → `ACTIVE`.

    **This is a display value, never an authorization decision.** `BURN_SQL` compares
    against PostgreSQL's `now()`; this compares against the calling process's clock.
    They are the same host and agree to within milliseconds, but the database clock
    is the authority and the conditional UPDATE is the only gate.
    """
    if row.used_at is not None:
        return EnrollmentTokenStatus.USED
    if row.revoked_at is not None:
        return EnrollmentTokenStatus.REVOKED
    if row.expires_at <= now:
        return EnrollmentTokenStatus.EXPIRED
    return EnrollmentTokenStatus.ACTIVE


async def create_enrollment_token(
    session: AsyncSession,
    *,
    group_id: uuid.UUID | None,
    expires_at: dt.datetime,
) -> tuple[EnrollmentToken, str]:
    """Mint an `ffe_` token, add its row to `session`, and return both halves.

    The plaintext comes back as a **separate** value rather than on the row, so it is
    visible at every call site that it is not persisted and must never be logged: it
    exists exactly once, in the issuance response body (`spec/flows.md` Flow 1 step 1
    — a human copies it into the flasher's baked config).

    `expires_at` is passed in rather than computed here. The 24 h number lives in
    `config.enrollment_token_ttl_hours` and is applied by the caller, which keeps this
    package free of both the settings object and the clock (`fleetforge.clock.now_utc`,
    applied by the router). It also lets a test build an already-expired token directly.

    The row is flushed but **not committed**; the caller owns the transaction.
    """
    issued = await aissue_token(ENROLLMENT_TOKEN_PREFIX)
    row = EnrollmentToken(
        id=issued.token_id,
        secret_hash=issued.secret_hash,
        group_id=group_id,
        expires_at=expires_at,
    )
    session.add(row)
    await session.flush()
    return row, issued.token

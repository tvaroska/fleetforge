"""Device boot/enrol progress — `POST /v1/device-progress`. S0-fw-1.

The second unauthenticated write endpoint, and a **deliberate sibling** of
`routers/enroll.py` rather than a copy of it. A board that is still booting has no
MQTT credential — that is what it is trying to get — so the channel cannot be MQTT and
the credential can only be the `ffe_` enrollment token it was flashed with.

> **Spec status: PROPOSED, not written.** `spec/device-protocol.md` describes the
> device wire contract and does not yet contain this endpoint. The proposal is
> recorded in `DECISIONS.md` (2026-09-10); this code is the implementation of the
> proposal, and `spec/` stays the frozen v1 contract until it is accepted.

The rule that makes it safe, and the difference from every line of `enroll.py`:

**The token is verified and NEVER burned.** `auth.enrollment.BURN_SQL` is not imported
here and must never be. Reporting a stage is not enrolling, and a board reports
`enrolling` several times before it succeeds.

Which follows into the one non-obvious authorization decision: an **already burned**
token is still accepted, but only from `used_by_device_id = device_id` and only while
`expires_at > now()`. Without that, the two most valuable stages — `enrolled` and
`mqtt_connected`, which by definition happen *after* the burn — could never be
reported at all. It widens nothing: the predicate names one device, the endpoint
issues no credential, provisions nothing and touches no `devices` row, so what it
grants is the right to say a stage and nothing else.

Everything else is `enroll.py`'s shape, for `enroll.py`'s reasons: rate-limit before
parsing, `dummy_verify()` on an unknown id so it costs what a wrong secret costs, one
401 body that never says *why*, and the reason in the log with the token *id* only.

202, not 200: the report is recorded, and the board is told nothing it could act on —
it must never wait on this endpoint or retry it (`agent/main/ff_progress.c`).
"""

import logging
from typing import Annotated

from fastapi import APIRouter, Depends, HTTPException, Request, Response, status

from fleetforge.api.deps import (
    SessionMakerDep,
    SettingsDep,
    client_key,
    now_utc,
    progress_cache,
    progress_limiter,
)
from fleetforge.api.schemas import ProgressReport
from fleetforge.auth.cache import VerifiedSecretCache
from fleetforge.auth.hashing import averify_secret, dummy_verify
from fleetforge.auth.ratelimit import FixedWindowLimiter
from fleetforge.auth.tokens import ENROLLMENT_TOKEN_PREFIX, parse_token
from fleetforge.db.models import EnrollmentToken
from fleetforge.events import DeviceEvent, EventType, emit
from fleetforge.progress import record_progress

logger = logging.getLogger(__name__)

router = APIRouter(prefix="/v1", tags=["enrollment"])

LimiterDep = Annotated[FixedWindowLimiter, Depends(progress_limiter)]
CacheDep = Annotated[VerifiedSecretCache, Depends(progress_cache)]

# Same body for every refusal, same reason as `enroll.py`: the wire says nothing about
# which of unknown / wrong-secret / revoked / expired / wrong-device it was.
INVALID_TOKEN = HTTPException(
    status_code=status.HTTP_401_UNAUTHORIZED, detail="invalid enrollment token"
)


@router.post(
    "/device-progress",
    status_code=status.HTTP_202_ACCEPTED,
    response_class=Response,
    summary="Report a boot or enrolment stage under an enrollment token",
)
async def report_progress(
    request: Request,
    body: ProgressReport,
    settings: SettingsDep,
    sessionmaker: SessionMakerDep,
    limiter: LimiterDep,
    cache: CacheDep,
) -> Response:
    """Record one stage report. Verifies the token; never burns, issues or provisions."""
    key = client_key(request)

    # Before any parsing and any hashing — an unauthenticated argon2 endpoint on the
    # public internet is free CPU otherwise. Unlike enroll, EVERY request counts and
    # not just failures: a board reporting correctly at 10 Hz is also a problem, and
    # the per-IP budget (60/min) is far above what an honest boot needs.
    if not limiter.allow(key):
        retry_after = limiter.retry_after(key)
        logger.warning("progress report rate-limited for %s", key)
        raise HTTPException(
            status_code=status.HTTP_429_TOO_MANY_REQUESTS,
            detail="too many progress reports",
            headers={"Retry-After": str(retry_after)},
        )
    # Counted here, unconditionally, before the work. `record_failure` is the
    # limiter's only "count one attempt" verb (`auth/ratelimit.py`) — the name reads
    # oddly on this endpoint precisely because this is the one caller that counts
    # successes too.
    limiter.record_failure(key)

    device_id = body.device_id
    parts = parse_token(body.token, ENROLLMENT_TOKEN_PREFIX)
    if parts is None:
        # An `ffa_` admin token lands here: the prefix check is what stops a progress
        # endpoint from becoming a second place to present the credential that matters.
        logger.info("progress rejected for %s: no usable ffe_ token presented", device_id)
        raise INVALID_TOKEN

    now = now_utc()

    async with sessionmaker() as session:
        row = await session.get(EnrollmentToken, parts.token_id)
        if row is None:
            await dummy_verify()
            logger.info("progress rejected: unknown token id %s", parts.token_id)
            raise INVALID_TOKEN

        # Cheap state checks BEFORE the hash, exactly as `require_admin` orders them:
        # a revoked or expired token is instantly dead and cannot burn CPU.
        if row.revoked_at is not None:
            logger.info("progress rejected: token %s is revoked", parts.token_id)
            raise INVALID_TOKEN

        if row.expires_at <= now:
            logger.info("progress rejected: token %s is expired", parts.token_id)
            raise INVALID_TOKEN

        # The burned case — see the module docstring. `used_by_device_id` is NOT NULL
        # whenever `used_at` is (they are written by the same UPDATE), but read it
        # defensively: `!=` against None would accept any device.
        if row.used_at is not None and row.used_by_device_id != device_id:
            logger.info("progress rejected: token %s was used by another device", parts.token_id)
            raise INVALID_TOKEN

        if not cache.check(parts.token_id, parts.secret):
            if not await averify_secret(parts.secret, row.secret_hash):
                logger.info("progress rejected: bad secret for token %s", parts.token_id)
                raise INVALID_TOKEN
            cache.remember(parts.token_id, parts.secret, ttl=settings.verify_cache_ttl_s)

        await record_progress(
            session,
            device_id=device_id,
            token_id=parts.token_id,
            stage=body.stage,
            detail=body.detail,
            max_rows_per_device=settings.progress_max_rows_per_device,
        )

        await emit(
            session,
            DeviceEvent(
                type=EventType.DEVICE_PROGRESS,
                device_id=device_id,
                at=now,
                # Always false, and honestly so: a board reporting a boot stage is by
                # definition not yet a fleet member with derived presence. The client
                # re-reads `GET /v1/devices` for the real answer, as for every type.
                online=False,
            ),
        )
        await session.commit()

    # The stage is `[a-z0-9_]` by validation and `detail` has no control characters, so
    # neither can forge a log line. `detail` is still device-controlled text.
    logger.info("device %s reports stage %s (%s)", device_id, body.stage, body.detail or "-")
    return Response(status_code=status.HTTP_202_ACCEPTED)

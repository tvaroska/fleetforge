"""Device enrollment — `POST /v1/enroll`. **CRITICAL.**

The **device-facing** half of the enrollment credential. Its admin-facing sibling
(`routers/enrollment.py`, note the name) issues, lists and revokes the tokens this
endpoint burns. `spec/device-protocol.md` → *Enrolment happens over HTTPS, not MQTT*
is the exchange, verbatim; this is steps 2 → 4 of it.

It is the **first and only unauthenticated write endpoint** in the API — the token in
the body *is* the credential — and it is the codebase's only path into `devices`
(`fleetforge.registry`).

Four orderings, each preventing a specific disaster:

1. **Verify the token secret before burning.** `BURN_SQL` keys on `id` alone, and an
   `ffe_` token's id is *not* a secret: it is in the issuance response body and in the
   api log. Burning before `averify_secret` would let anyone who has read a log line
   destroy every outstanding token — a bench full of boards that will not enroll, with
   the dashboard reporting them `used` and nothing failing loudly. The order is
   `deps.require_admin`'s: parse → row → `dummy_verify` on a miss → verify → burn.
2. **Insert the device before the burn, in the same transaction.**
   `enrollment_tokens.used_by_device_id` is a real FK, so burning first is an
   `IntegrityError` on the happy path — and a refused burn must roll the device row
   back, or a rejected enrollment leaves a fleet member behind.
3. **Reject a bad identity before the burn** (`schemas.EnrollRequest`). A burn
   followed by a DB CHECK violation is a token destroyed by a firmware typo.
4. **Commit before provisioning the broker.** `db/models.py::Device` put
   `broker_provisioned_at` in the schema "so provisioning can be reconciled and
   retried idempotently after a partial enrollment" — the schema already chose this.
   Holding a row lock and a pooled connection across an MQTT round-trip turns a broker
   outage into `idle in transaction` on a 256 M container. The inverse failure — a
   broker credential for a device that is not enrolled — is prevented by the *order*,
   not by a transaction.

Nothing here logs the broker password, the token plaintext, or the secret half.
"""

import logging
from typing import Annotated

from fastapi import APIRouter, Depends, HTTPException, Request, status
from sqlalchemy import text
from sqlalchemy.exc import IntegrityError, SQLAlchemyError
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from fleetforge.api.deps import (
    BrokerDep,
    SessionMakerDep,
    SettingsDep,
    client_key,
    enroll_limiter,
    now_utc,
)
from fleetforge.api.schemas import EnrollRequest, EnrollResponse
from fleetforge.auth.enrollment import BURN_SQL, RETRY_LOOKUP_SQL
from fleetforge.auth.hashing import averify_secret, dummy_verify
from fleetforge.auth.ratelimit import FixedWindowLimiter
from fleetforge.auth.tokens import ENROLLMENT_TOKEN_PREFIX, parse_token
from fleetforge.broker import BrokerProvisioningError, generate_broker_password
from fleetforge.db.models import EnrollmentToken
from fleetforge.events import DeviceEvent, EventType, emit
from fleetforge.presence import is_online
from fleetforge.registry import IDENTITY_FIELDS, enroll_device

logger = logging.getLogger(__name__)

router = APIRouter(prefix="/v1", tags=["enrollment"])

LimiterDep = Annotated[FixedWindowLimiter, Depends(enroll_limiter)]

# No `WWW-Authenticate`: the token in the body is not an HTTP auth scheme. As with
# `require_admin`, nothing in the response says *why* — the log distinguishes.
INVALID_TOKEN = HTTPException(
    status_code=status.HTTP_401_UNAUTHORIZED, detail="invalid enrollment token"
)

# One body for used-by-another / revoked / expired. The caller has already proved
# possession of the secret, but the dashboard — not the wire — is where an operator
# diagnoses a dead token, and three distinct messages would only help someone probing.
TOKEN_UNUSABLE = HTTPException(
    status_code=status.HTTP_409_CONFLICT, detail="enrollment token is not usable"
)

UNKNOWN_PARENT = HTTPException(
    status_code=status.HTTP_400_BAD_REQUEST, detail="unknown parent device"
)

# Stamped only once the broker really holds the credential (`ensure_client` → True).
# A separate, tiny transaction: the enrollment is already committed by then.
MARK_PROVISIONED_SQL = text(
    "UPDATE devices SET broker_provisioned_at = now() WHERE device_id = :device_id"
)


@router.post(
    "/enroll",
    response_model=EnrollResponse,
    status_code=status.HTTP_200_OK,
    summary="Exchange a single-use enrollment token for a broker credential",
)
async def enroll(
    request: Request,
    body: EnrollRequest,
    settings: SettingsDep,
    sessionmaker: SessionMakerDep,
    provisioner: BrokerDep,
    limiter: LimiterDep,
) -> EnrollResponse:
    """`spec/device-protocol.md` steps 2 → 4: token in, broker credential out.

    **Unauthenticated by design** — this is the endpoint a board with no credential
    calls to get one, so hanging it off `AdminDep` would make enrollment impossible.
    An `ffa_` admin token in the `token` field is rejected: `parse_token` checks the
    prefix, and an enrollment endpoint that accepts an admin credential is a second
    way to spend the one that matters.

    200 rather than 201 for both the first burn and the grace-window retry (§ below):
    the retry re-issues a credential for a row that already exists, a 201 there would
    be a lie, and the agent gets one branch instead of two.
    """
    key = client_key(request)

    # Before any parsing and any hashing: an unauthenticated argon2 endpoint on the
    # public internet is free CPU for an attacker otherwise. Only failures are
    # counted, so 25 boards behind one NAT rebooting together cannot lock themselves
    # out (`auth/ratelimit.py`).
    if not limiter.allow(key):
        retry_after = limiter.retry_after(key)
        logger.warning("enrollment rate-limited for %s", key)
        raise HTTPException(
            status_code=status.HTTP_429_TOO_MANY_REQUESTS,
            detail="too many enrollment attempts",
            headers={"Retry-After": str(retry_after)},
        )

    device_id = body.device_id
    parts = parse_token(body.token, ENROLLMENT_TOKEN_PREFIX)
    if parts is None:
        limiter.record_failure(key)
        logger.info("enrollment rejected for %s: no usable ffe_ token presented", device_id)
        raise INVALID_TOKEN

    if body.proto != 1:
        # Stored, never rejected: "the server must tolerate agents it cannot update."
        logger.info("device %s announces proto %s at enrollment", device_id, body.proto)

    async with sessionmaker() as session:
        row = await session.get(EnrollmentToken, parts.token_id)
        if row is None:
            # Burn a verification so an unknown id costs about what a wrong secret does.
            await dummy_verify()
            limiter.record_failure(key)
            logger.info("enrollment rejected: unknown token id %s", parts.token_id)
            raise INVALID_TOKEN

        # THE ORDER THAT MATTERS: the secret is verified before anything can burn.
        if not await averify_secret(parts.secret, row.secret_hash):
            limiter.record_failure(key)
            logger.info("enrollment rejected: bad secret for token %s", parts.token_id)
            raise INVALID_TOKEN

        group_id = row.group_id
        now = now_utc()

        try:
            device = await enroll_device(
                session,
                device_id=device_id,
                # The group comes from the TOKEN. A device never chooses its own.
                group_id=group_id,
                identity={field: getattr(body, field) for field in IDENTITY_FIELDS},
                enrolled_at=now,
            )
        except IntegrityError:
            # The only FK the body can violate is `parent_device_id`. Check-then-insert
            # would still be a race, so the insert itself is the check — the same shape
            # `routers/enrollment.py` uses for `group_id`.
            await session.rollback()
            logger.info(
                "enrollment refused for %s: unknown parent device %s",
                device_id,
                body.parent_device_id,
            )
            raise UNKNOWN_PARENT from None

        burned = (
            await session.execute(BURN_SQL, {"token_id": parts.token_id, "device_id": device_id})
        ).scalar_one_or_none()

        if burned is None:
            # Zero rows: already burned, revoked, or expired. The ONE narrow exception
            # is the grace window — the same device re-presenting the same token after
            # a lost response. It matches on `used_by_device_id`, so a token still
            # enrolls exactly one board, forever.
            retry = (
                await session.execute(
                    RETRY_LOOKUP_SQL,
                    {
                        "token_id": parts.token_id,
                        "device_id": device_id,
                        "window_s": settings.enroll_retry_window_s,
                    },
                )
            ).scalar_one_or_none()
            if retry is None:
                # The device row must NOT survive a refused enrollment.
                await session.rollback()
                limiter.record_failure(key)
                logger.info(
                    "enrollment refused for %s: token %s is used, revoked or expired",
                    device_id,
                    parts.token_id,
                )
                raise TOKEN_UNUSABLE
            logger.warning(
                "enrollment retry within the %ds grace window: device %s token %s",
                settings.enroll_retry_window_s,
                device_id,
                parts.token_id,
            )

        await emit(
            session,
            DeviceEvent(
                type=EventType.DEVICE_ENROLLED,
                device_id=device_id,
                at=now,
                # One rule, every reader — never a hardcoded False. On a fresh row it
                # evaluates to False for both power classes, which is correct: the
                # board has not connected yet.
                online=is_online(device, now=now, tolerance=settings.presence_tolerance),
                fw_version=device.fw_version,
            ),
        )
        await session.commit()

    # Committed. Only now does a broker credential get created — see docstring (4).
    password = generate_broker_password()
    try:
        provisioned = await provisioner.ensure_client(device_id, password)
    except BrokerProvisioningError as exc:
        logger.error(
            "broker provisioning failed for %s: %s. The enrollment is committed and the "
            "token is burned; reconcile with: broker_provisioned_at IS NULL",
            device_id,
            exc,
        )
        raise HTTPException(
            status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
            detail="broker provisioning unavailable",
            headers={"Retry-After": "5"},
        ) from None

    if provisioned:
        await _mark_provisioned(sessionmaker, device_id)

    logger.info(
        "enrolled device %s (token %s, group %s, broker_provisioned=%s)",
        device_id,
        parts.token_id,
        group_id,
        provisioned,
    )
    # `mqtt_username` is the device_id, unnormalised: the `%u` pattern ACLs bind to it.
    return EnrollResponse(device_id=device_id, mqtt_username=device_id, mqtt_password=password)


async def _mark_provisioned(sessionmaker: async_sessionmaker[AsyncSession], device_id: str) -> None:
    """Stamp `broker_provisioned_at`. A failure here must not fail the request.

    The broker already holds the credential and the device must get its password, so
    the worst case is a row that is *under*-reported — which is the safe direction for
    `R0-sec-1`'s `WHERE broker_provisioned_at IS NULL` reconcile query.
    """
    try:
        async with sessionmaker() as session:
            await session.execute(MARK_PROVISIONED_SQL, {"device_id": device_id})
            await session.commit()
    except (SQLAlchemyError, OSError) as exc:
        logger.error(
            "device %s is provisioned on the broker but broker_provisioned_at was not "
            "stamped (%s); it will show up in the reconcile query",
            device_id,
            type(exc).__name__,
        )

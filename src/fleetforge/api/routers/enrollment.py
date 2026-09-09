"""Enrollment token issuance — `/v1/enrollment-tokens`. **CRITICAL.**

The admin-facing half of the enrollment credential: issue, list, revoke. The
device-facing half (`POST /v1/enroll`, which validates a presented token and **burns**
it) is `R0-be-4` and imports `BURN_SQL` from `fleetforge.auth.enrollment` rather than
writing its own.

Three properties this module exists to hold:

* **The plaintext is returned exactly once**, in the `POST` body, because a human
  copies it into the flasher's baked config (`spec/flows.md` Flow 1). Nothing stores
  it, nothing logs it, and no endpoint can retrieve it afterwards — `GET` has no field
  that could carry it.
* **The reported `status` is the burn predicate** (`auth/enrollment.token_status`), so
  the dashboard can never say "active" about a token that will not enroll.
* **Revocation is a conditional UPDATE**, never a read-then-write, and it takes effect
  against the burn immediately.

`POST …/revoke` rather than `DELETE …`: the row survives revocation. `spec/prd.md` →
*Retention* keeps revoked tokens 90 days, and `used_by_device_id` is the fleet's
enrollment provenance — a `DELETE` verb would invite someone to actually delete it.
"""

import datetime as dt
import logging
import uuid

from fastapi import APIRouter, Depends, HTTPException, status
from sqlalchemy import select, text
from sqlalchemy.exc import IntegrityError

from fleetforge.api.deps import (
    AdminDep,
    SessionMakerDep,
    SettingsDep,
    bearer_scheme,
    cookie_scheme,
    now_utc,
)
from fleetforge.api.schemas import (
    EnrollmentTokenCreate,
    EnrollmentTokenIssued,
    EnrollmentTokenList,
    EnrollmentTokenSummary,
)
from fleetforge.auth.enrollment import REVOKE_SQL, create_enrollment_token, token_status
from fleetforge.db.models import EnrollmentToken

logger = logging.getLogger(__name__)

router = APIRouter(
    prefix="/v1/enrollment-tokens",
    tags=["enrollment"],
    dependencies=[Depends(bearer_scheme), Depends(cookie_scheme)],
)

GROUP_EXISTS_SQL = text("SELECT 1 FROM device_groups WHERE id = :group_id")

# `spec/prd.md` → *Capacity* sizes v1 at ~25 devices, so the history is small and the
# cap is a guard against an unbounded response rather than real paging. Pagination
# arrives with R1's 90-day purge, if it is still needed by then.
LIST_LIMIT = 200

UNKNOWN_GROUP = HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="unknown group")
UNKNOWN_TOKEN = HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="unknown token")


@router.post(
    "",
    response_model=EnrollmentTokenIssued,
    status_code=status.HTTP_201_CREATED,
    summary="Issue an enrollment token (the plaintext is shown once)",
)
async def issue_enrollment_token(
    body: EnrollmentTokenCreate,
    admin: AdminDep,
    settings: SettingsDep,
    sessionmaker: SessionMakerDep,
) -> EnrollmentTokenIssued:
    """Mint a single-use `ffe_` token valid for `enrollment_token_ttl_hours`.

    The returned `token` is the only copy: what lands in the database is an argon2id
    hash of the secret half. Reissue is the recovery path for a lost token; there is
    no retrieval.

    An unknown `group_id` is a `404`, not the `500` an escaping FK `IntegrityError`
    would produce. The existence check and the insert are two statements, so the
    insert is also guarded — the group could be deleted between them.
    """
    expires_at = now_utc() + dt.timedelta(hours=settings.enrollment_token_ttl_hours)

    async with sessionmaker() as session:
        if body.group_id is not None:
            exists = await session.scalar(GROUP_EXISTS_SQL, {"group_id": body.group_id})
            if exists is None:
                logger.info("enrollment token refused: unknown group %s", body.group_id)
                raise UNKNOWN_GROUP

        try:
            row, plaintext = await create_enrollment_token(
                session, group_id=body.group_id, expires_at=expires_at
            )
            await session.commit()
        except IntegrityError:
            # The only FK on the insert is `group_id`: the group was deleted between
            # the check above and the commit. Benign, and the same answer.
            logger.info("enrollment token refused: group %s vanished mid-insert", body.group_id)
            raise UNKNOWN_GROUP from None

    # Ids only. Never the token, never the secret half, never the PHC string.
    logger.info(
        "issued enrollment token %s (group %s) by %s", row.id, body.group_id, admin.token_id
    )
    return EnrollmentTokenIssued(
        id=row.id,
        token=plaintext,
        group_id=row.group_id,
        expires_at=row.expires_at,
        created_at=row.created_at,
    )


@router.get(
    "",
    response_model=EnrollmentTokenList,
    summary="Issued enrollment tokens, newest first",
)
async def list_enrollment_tokens(
    admin: AdminDep,
    sessionmaker: SessionMakerDep,
) -> EnrollmentTokenList:
    """The issuance history, with each row's derived status.

    Capped at `LIST_LIMIT`. The summaries are built field by field so that no future
    change to the ORM model can leak `secret_hash` into a response, and `status` is
    computed against **one** `now` for the whole response — a list where two rows
    disagree about the current time is a confusing thing to debug.
    """
    now = now_utc()
    async with sessionmaker() as session:
        rows = (
            await session.scalars(
                select(EnrollmentToken)
                .order_by(EnrollmentToken.created_at.desc(), EnrollmentToken.id.desc())
                .limit(LIST_LIMIT)
            )
        ).all()

    return EnrollmentTokenList(
        tokens=[
            EnrollmentTokenSummary(
                id=row.id,
                group_id=row.group_id,
                status=token_status(row, now),
                created_at=row.created_at,
                expires_at=row.expires_at,
                used_at=row.used_at,
                used_by_device_id=row.used_by_device_id,
                revoked_at=row.revoked_at,
            )
            for row in rows
        ]
    )


@router.post(
    "/{token_id}/revoke",
    status_code=status.HTTP_204_NO_CONTENT,
    summary="Revoke an enrollment token (idempotent)",
)
async def revoke_enrollment_token(
    token_id: uuid.UUID,
    admin: AdminDep,
    sessionmaker: SessionMakerDep,
) -> None:
    """Kill a token. It can never be burned afterwards.

    One conditional UPDATE, so a second call does not move `revoked_at`. `RETURNING
    id` gives back nothing when the token is either unknown or already revoked; only
    then is a second query needed, purely to choose between `404` and an idempotent
    `204`.

    Revoking an already-**used** token is allowed and is a no-op in effect: the burn
    already failed the `used_at IS NULL` clause. It is not an error.
    """
    async with sessionmaker() as session:
        revoked = (await session.execute(REVOKE_SQL, {"token_id": token_id})).scalar_one_or_none()
        await session.commit()

        if revoked is None:
            if await session.get(EnrollmentToken, token_id) is None:
                logger.info("revoke refused: unknown enrollment token %s", token_id)
                raise UNKNOWN_TOKEN
            logger.info("enrollment token %s was already revoked", token_id)
            return

    logger.info("revoked enrollment token %s by %s", token_id, admin.token_id)

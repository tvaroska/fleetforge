"""Human user account management — `/v1/users`.

Superadmin-only CRUD for creating collaborators, rotating passwords, and disabling
accounts (`docs/features/projects-and-users.md`). Disabling a user sets `disabled_at`
and revokes all of their active tokens so `require_admin` rejects them on the very
next request.
"""

import logging
from typing import Annotated

import anyio.to_thread
from fastapi import APIRouter, Depends, HTTPException, Path, status
from sqlalchemy import select, text
from sqlalchemy.exc import IntegrityError

from fleetforge.api.deps import (
    FORBIDDEN,
    AdminDep,
    FirebaseAuthDep,
    SessionMakerDep,
    SettingsDep,
    bearer_scheme,
    cookie_scheme,
    now_utc,
    token_cache,
)
from fleetforge.api.schemas import (
    USERNAME_PATTERN,
    EmailDelivery,
    UserCreate,
    UserList,
    UserSummary,
    UserUpdate,
)
from fleetforge.auth.cache import VerifiedSecretCache
from fleetforge.auth.firebase import FirebaseAuthClient, FirebaseAuthError
from fleetforge.auth.hashing import ARGON2_LIMITER, hash_secret
from fleetforge.config import Settings
from fleetforge.db.models import AdminToken, User

logger = logging.getLogger(__name__)

router = APIRouter(
    prefix="/v1/users",
    tags=["users"],
    dependencies=[Depends(bearer_scheme), Depends(cookie_scheme)],
)

CacheDep = Annotated[VerifiedSecretCache, Depends(token_cache)]

_REVOKE_USER_TOKENS_SQL = text(
    "UPDATE admin_tokens SET revoked_at = now() "
    "WHERE user_id = :user_id AND revoked_at IS NULL RETURNING id"
)


def _user_summary(user: User) -> UserSummary:
    return UserSummary(
        id=user.id,
        username=user.username,
        email=user.email,
        firebase_uid=user.firebase_uid,
        has_local_password=user.password_hash is not None,
        display_name=user.display_name,
        is_superadmin=user.is_superadmin,
        disabled_at=user.disabled_at,
        created_at=user.created_at,
    )


async def _send_setup_email(
    firebase: FirebaseAuthClient, settings: Settings, email: str
) -> EmailDelivery:
    """Make sure `email` has a Firebase account, then send it the password-setup email.

    Firebase answers `EMAIL_NOT_FOUND` to a reset for an address with no account, so an
    invite creates the account first (random password, never stored; the reset email
    lets the real mailbox owner set theirs). An existing account, possibly one a
    squatter registered, is left in place: the reset still goes to the real mailbox.
    """
    api_key = settings.firebase_web_api_key
    if not api_key:
        return "not_configured"
    try:
        await firebase.create_account(email, api_key=api_key)
        await firebase.send_password_reset(email, api_key=api_key)
    except FirebaseAuthError:
        logger.warning("firebase setup email could not be sent for %s", email)
        return "failed"
    return "sent"


async def _ahash_password(password: str) -> str:
    return await anyio.to_thread.run_sync(hash_secret, password, limiter=ARGON2_LIMITER)


@router.get(
    "",
    response_model=UserList,
    summary="List human user accounts (superadmin only)",
)
async def list_users(admin: AdminDep, sessionmaker: SessionMakerDep) -> UserList:
    if not admin.is_superadmin or "admin" not in admin.scopes:
        raise FORBIDDEN

    async with sessionmaker() as session:
        rows = (await session.scalars(select(User).order_by(User.username))).all()
    return UserList(users=[_user_summary(u) for u in rows])


@router.post(
    "",
    status_code=status.HTTP_201_CREATED,
    response_model=UserSummary,
    summary="Invite or create a human user account (superadmin only)",
)
async def create_user(
    body: UserCreate,
    admin: AdminDep,
    sessionmaker: SessionMakerDep,
    settings: SettingsDep,
    firebase: FirebaseAuthDep,
) -> UserSummary:
    if not admin.is_superadmin or "admin" not in admin.scopes:
        raise FORBIDDEN

    pw_hash = await _ahash_password(body.password) if body.password is not None else None
    now = now_utc()
    user = User(
        username=body.username,
        email=body.email,
        display_name=body.display_name,
        password_hash=pw_hash,
        is_superadmin=body.is_superadmin,
        created_at=now,
        updated_at=now,
    )
    async with sessionmaker() as session:
        session.add(user)
        try:
            await session.commit()
        except IntegrityError as exc:
            await session.rollback()
            raise HTTPException(
                status_code=status.HTTP_409_CONFLICT,
                detail=f"user {body.username!r} or email {body.email!r} already exists",
            ) from exc

    delivery: EmailDelivery | None = None
    if body.send_invite_email and body.email:
        delivery = await _send_setup_email(firebase, settings, body.email)

    logger.info(
        "user invited/created: %s (email=%s, id=%s, email_delivery=%s) by %s",
        user.username,
        user.email,
        user.id,
        delivery,
        admin.subject,
    )
    return _user_summary(user).model_copy(update={"email_delivery": delivery})


@router.patch(
    "/{username}",
    response_model=UserSummary,
    summary="Update a user's email, password, display_name, superadmin flag, or disabled state",
)
async def update_user(
    username: str = Path(pattern=USERNAME_PATTERN),
    *,
    body: UserUpdate,
    admin: AdminDep,
    sessionmaker: SessionMakerDep,
    settings: SettingsDep,
    firebase: FirebaseAuthDep,
    cache: CacheDep,
) -> UserSummary:
    if "admin" not in admin.scopes:
        raise FORBIDDEN

    is_self = admin.subject == username and admin.user_id is not None
    if not admin.is_superadmin:
        if not is_self or body.is_superadmin is not None or body.disabled is not None:
            raise FORBIDDEN

    pw_hash = await _ahash_password(body.password) if body.password is not None else None
    revoked_ids: list[object] = []

    async with sessionmaker() as session:
        user = await session.scalar(select(User).where(User.username == username))
        if user is None:
            raise HTTPException(
                status_code=status.HTTP_404_NOT_FOUND,
                detail=f"user {username!r} not found",
            )
        if "email" in body.model_fields_set:
            user.email = body.email
        if "display_name" in body.model_fields_set:
            user.display_name = body.display_name
        if pw_hash is not None:
            user.password_hash = pw_hash
        if body.is_superadmin is not None:
            user.is_superadmin = body.is_superadmin
        if body.disabled is not None:
            if body.disabled:
                user.disabled_at = now_utc()
                res = await session.execute(_REVOKE_USER_TOKENS_SQL, {"user_id": user.id})
                revoked_ids = list(res.scalars().all())
            else:
                user.disabled_at = None
        user.updated_at = now_utc()
        try:
            await session.commit()
        except IntegrityError as exc:
            await session.rollback()
            raise HTTPException(
                status_code=status.HTTP_409_CONFLICT,
                detail=f"email {body.email!r} is already in use",
            ) from exc
        target_email = user.email
        summary = _user_summary(user)

    for tid in revoked_ids:
        if isinstance(tid, AdminToken.id.type.python_type):
            cache.forget(tid)

    if body.send_password_reset and target_email:
        delivery = await _send_setup_email(firebase, settings, target_email)
        summary = summary.model_copy(update={"email_delivery": delivery})

    return summary

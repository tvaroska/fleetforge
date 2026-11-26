"""Shared API router helpers so routers never import sibling routers (`A-29`)."""

from __future__ import annotations

import base64
import binascii
import hashlib
import hmac

from fastapi import HTTPException, status
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from fleetforge.api.deps import AuthContext
from fleetforge.api.schemas import CommandSummary
from fleetforge.api.schemas import DeploySender as DeploySenderSchema
from fleetforge.db.models import AdminToken, Device
from fleetforge.deploys import DeploySender
from fleetforge.device_commands import CommandSnapshot
from fleetforge.identity import is_valid_device_id

INVALID_DEVICE_CREDENTIAL = HTTPException(
    status_code=status.HTTP_401_UNAUTHORIZED,
    detail="invalid device credentials",
    headers={"WWW-Authenticate": "Basic"},
)


async def resolve_deploy_sender(session: AsyncSession, admin: AuthContext) -> DeploySender:
    """Resolve the sender record via one PK read for the token's label."""
    token = await session.get(AdminToken, admin.token_id)
    return DeploySender(
        subject=admin.subject,
        token_id=str(admin.token_id),
        credential=token.name if token is not None else None,
    )


def command_summary(snapshot: CommandSnapshot | None) -> CommandSummary | None:
    """A board's newest command and its first answer, or `None` (R5-be-2). Field by field."""
    if snapshot is None:
        return None
    return CommandSummary(
        command_id=snapshot.command_id,
        type=snapshot.type,
        cmd_id=snapshot.cmd_id,
        sent_at=snapshot.sent_at,
        sent_by=(
            DeploySenderSchema(
                subject=snapshot.sent_by.subject, credential=snapshot.sent_by.credential
            )
            if snapshot.sent_by is not None
            else None
        ),
        result=snapshot.result,
        detail=snapshot.detail,
        answered_at=snapshot.answered_at,
    )


def parse_device_basic_auth(auth_header: str | None) -> tuple[str, str] | None:
    """Extract `(device_id, mqtt_password)` from an HTTP Basic `Authorization` header."""
    if not auth_header:
        return None
    scheme, _, param = auth_header.strip().partition(" ")
    if scheme.lower() != "basic" or not param:
        return None
    try:
        decoded = base64.b64decode(param.strip(), validate=True).decode("utf-8")
    except (binascii.Error, UnicodeDecodeError):
        return None
    if ":" not in decoded:
        return None
    device_id, password = decoded.split(":", 1)
    if not is_valid_device_id(device_id) or not password:
        return None
    return device_id, password


async def authenticate_device_basic(
    session: AsyncSession,
    auth_header: str | None,
) -> Device | None:
    """Authenticate an enrolled live device using HTTP Basic `device_id:mqtt_password`."""
    parsed = parse_device_basic_auth(auth_header)
    if parsed is None:
        return None
    device_id, password = parsed
    candidate_hash = hashlib.sha256(password.encode("utf-8")).hexdigest()
    device = (
        await session.scalars(
            select(Device).where(
                Device.device_id == device_id,
                Device.decommissioned_at.is_(None),
            )
        )
    ).one_or_none()
    if (
        device is None
        or not device.mqtt_password_hash
        or not hmac.compare_digest(candidate_hash, device.mqtt_password_hash)
    ):
        return None
    return device

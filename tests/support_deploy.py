"""Shared HTTP/DB deploy and command test fixtures and helpers (`T-11`)."""

from __future__ import annotations

import hashlib
from typing import Any

import httpx
from fastapi import FastAPI
from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncSession

from fleetforge.clock import now_utc
from fleetforge.db.models import Device
from fleetforge.signing import generate_keypair, sign_digest
from tests.conftest import client_for, settings_for_tests

DEVICE_ID = "a4cf12b3de90"
OTHER_DEVICE_ID = "a4cf12b3de91"
IMAGE = b"\xe9\x06\x02\x20" + b"firmware bytes, opaque to the server" * 37
OTHER_IMAGE = IMAGE + b"!"
SHA256 = hashlib.sha256(IMAGE).hexdigest()
OTHER_SHA256 = hashlib.sha256(OTHER_IMAGE).hexdigest()
RELEASE_PRIV_1, RELEASE_PUB_1 = generate_keypair()
RELEASE_PRIV_2, RELEASE_PUB_2 = generate_keypair()
TEST_RELEASE_PUBLIC_RING = f"{RELEASE_PUB_1};{RELEASE_PUB_2}"
TEST_SIG = sign_digest(SHA256, RELEASE_PRIV_1)
OTHER_TEST_SIG = sign_digest(OTHER_SHA256, RELEASE_PRIV_1)
VERSION = "1.5.0"
TARGET = "esp32"
LAYOUT = "ab-4m-v1"
AB_SHA = "1fa67e6bbd034e434d04e9d6f4f52bbe899361602cd498573eb3bde97d1559ed"
ARDUINO_SHA = "05528998ae17fb6a7a5741443f9a7a4720c766f370fefc30814cbc3e391c1fc4"
WRONG_SHA = "47db53920359cfb4581532a293d8e563401f3abe37f9b282cc713039ac937c4c"
TTL_S = settings_for_tests().signed_url_ttl_s
CONFIRM_TIMEOUT_S = settings_for_tests().confirm_timeout_s
PUBLIC_BASE_URL = settings_for_tests().public_base_url

_DEFAULT_SIG = object()


async def add_device(session: AsyncSession, device_id: str = DEVICE_ID, **overrides: Any) -> Device:
    values: dict[str, Any] = {
        "device_id": device_id,
        "platform_type": TARGET,
        "link_type": "wifi",
        "power_class": "always_on",
        "fw_version": "1.4.2",
        "partition_layout": LAYOUT,
        "ota_slot_size": 1966080,
        "capabilities": ["ota"],
        "presence_reported": True,
        "last_seen": now_utc(),
    }
    values.update(overrides)
    device = Device(**values)
    session.add(device)
    await session.commit()
    return device


async def add_artifact(
    session: AsyncSession,
    *,
    sha256: str = SHA256,
    size_bytes: int = len(IMAGE),
    target: str = TARGET,
    version: str = VERSION,
    layout: str | None = LAYOUT,
    has_lib_marker: bool | None = None,
    sig: str | None | object = _DEFAULT_SIG,
) -> None:
    """Insert an artifact row and its `(target, version)` label directly."""
    resolved_sig: str | None
    if sig is _DEFAULT_SIG:
        if sha256 == SHA256:
            resolved_sig = TEST_SIG
        elif sha256 == OTHER_SHA256:
            resolved_sig = OTHER_TEST_SIG
        else:
            resolved_sig = sign_digest(sha256, RELEASE_PRIV_1)
    else:
        resolved_sig = sig  # type: ignore[assignment]
    await session.execute(
        text(
            "INSERT INTO artifacts (sha256, size_bytes, kind, target, partition_layout, "
            "has_lib_marker, sig) "
            "VALUES (:sha256, :size_bytes, 'user_firmware', :target, :layout, :marker, :sig) "
            "ON CONFLICT (sha256) DO NOTHING"
        ),
        {
            "sha256": sha256,
            "size_bytes": size_bytes,
            "target": target,
            "layout": layout,
            "marker": has_lib_marker,
            "sig": resolved_sig,
        },
    )
    await session.execute(
        text(
            "INSERT INTO artifact_versions (target, version, sha256) "
            "VALUES (:target, :version, :sha256)"
        ),
        {"target": target, "version": version, "sha256": sha256},
    )
    await session.commit()


async def deploy(
    app: FastAPI,
    token: str,
    device_id: str = DEVICE_ID,
    **body: Any,
) -> httpx.Response:
    payload: dict[str, Any] = {"version": VERSION}
    payload.update(body)
    async with client_for(app, base_url="https://testserver") as client:
        return await client.post(
            f"/v1/devices/{device_id}/deploy",
            json=payload,
            headers={"authorization": f"Bearer {token}"},
        )


async def precheck(
    app: FastAPI,
    token: str,
    device_id: str = DEVICE_ID,
    **body: Any,
) -> httpx.Response:
    payload: dict[str, Any] = {"version": VERSION}
    payload.update(body)
    async with client_for(app, base_url="https://testserver") as client:
        return await client.post(
            f"/v1/devices/{device_id}/deploy/precheck",
            json=payload,
            headers={"authorization": f"Bearer {token}"},
        )


async def events(session: AsyncSession) -> list[dict[str, Any]]:
    rows = (
        await session.execute(
            text(
                "SELECT cmd_id, state, is_terminal, from_version, artifact_version, detail "
                "FROM deploy_events ORDER BY id"
            )
        )
    ).all()
    return [dict(row._mapping) for row in rows]  # noqa: SLF001

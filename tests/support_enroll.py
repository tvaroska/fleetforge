"""Shared enrollment and progress HTTP/DB test helpers (`T-11`)."""

from __future__ import annotations

import uuid
from typing import Any

import httpx
from fastapi import FastAPI
from sqlalchemy import select, text
from sqlalchemy.ext.asyncio import AsyncEngine, AsyncSession, async_sessionmaker

from fleetforge.db.models import Device, EnrollmentToken
from tests.conftest import client_for, login_admin

BASE_URL = "https://testserver"
SHA = "1fa67e6bbd034e434d04e9d6f4f52bbe899361602cd498573eb3bde97d1559ed"


def sessionmaker_for(engine: AsyncEngine) -> async_sessionmaker[AsyncSession]:
    return async_sessionmaker(engine, expire_on_commit=False)


async def issue_token(app: FastAPI, **body: object) -> dict[str, Any]:
    """Mint an enrollment token through `POST /v1/enrollment-tokens`."""
    admin = await login_admin(app)
    async with client_for(app, base_url=BASE_URL) as client:
        client.headers["Authorization"] = f"Bearer {admin}"
        response = await client.post("/v1/enrollment-tokens", json=body)
    assert response.status_code == 201, response.text
    payload: dict[str, Any] = response.json()
    return payload


def enroll_body(token: str, device_id: str, **overrides: object) -> dict[str, Any]:
    """Build the flat enrollment body specified in `spec/device-protocol.md`."""
    body: dict[str, Any] = {
        "token": token,
        "device_id": device_id,
        "platform_type": "esp32c6",
        "link_type": "wifi",
        "power_class": "always_on",
        "proto": 1,
        "fw_version": "1.4.2",
        "agent_version": "0.3.0",
        "partition_layout": "ab-4m-v1",
        "ota_slot_size": 1966080,
        "capabilities": ["ota", "selftest"],
    }
    body.update(overrides)
    return body


async def post_enroll(app: FastAPI, body: dict[str, Any]) -> httpx.Response:
    """POST to `/v1/enroll` with no session or bearer credential."""
    async with client_for(app, base_url=BASE_URL) as client:
        return await client.post("/v1/enroll", json=body)


async def fetch_device(engine: AsyncEngine, device_id: str) -> Device | None:
    async with sessionmaker_for(engine)() as session:
        return await session.get(Device, device_id)


async def fetch_token(engine: AsyncEngine, token_id: str | uuid.UUID) -> EnrollmentToken:
    async with sessionmaker_for(engine)() as session:
        row = await session.get(EnrollmentToken, uuid.UUID(str(token_id)))
    assert row is not None
    return row


async def count_devices(engine: AsyncEngine, device_id: str) -> int:
    async with sessionmaker_for(engine)() as session:
        rows = (await session.scalars(select(Device).where(Device.device_id == device_id))).all()
    return len(rows)


async def execute(engine: AsyncEngine, statement: str, **params: object) -> None:
    async with sessionmaker_for(engine)() as session:
        await session.execute(text(statement), params)
        await session.commit()

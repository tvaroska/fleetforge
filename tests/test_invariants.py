"""The invariants must be enforced by the database, not by application discipline.

Every test here corresponds to a way the fleet gets hurt if the constraint is only a
convention: a token that can be burned twice enrolls an arbitrary fleet, a sleepy
device with no wake interval never shows offline, and a deleted device takes the KPI
evidence with it.
"""

import asyncio
import datetime as dt
from pathlib import Path

import pytest
from sqlalchemy import inspect, select, text
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncEngine, AsyncSession

from fleetforge.auth.enrollment import BURN_SQL
from fleetforge.db.models import (
    AdminToken,
    DeployEvent,
    Device,
    DeviceGroup,
    EnrollmentToken,
)

# BURN_SQL is imported, not copied: `fleetforge.auth.enrollment` ships the one
# statement R0-be-4 executes, so these tests exercise the shipped code path rather
# than a duplicate that could drift away from it (`DECISIONS.md` 2026-09-08 →
# *Enrollment token issuance: one predicate, two readers*).


def make_device(device_id: str = "a4cf12b3de90", **overrides: object) -> Device:
    values: dict[str, object] = {
        "device_id": device_id,
        "platform_type": "esp32c6",
        "link_type": "wifi",
        "power_class": "always_on",
    }
    values.update(overrides)
    return Device(**values)


async def test_device_id_must_be_lowercase_mac(session: AsyncSession) -> None:
    for bad_id in ("A4CF12B3DE90", "a4:cf:12:b3:de:90", "a4cf12b3de9", "zzzzzzzzzzzz"):
        with pytest.raises(IntegrityError):
            async with session.begin_nested():
                session.add(make_device(bad_id))
                await session.flush()

    async with session.begin_nested():
        session.add(make_device("a4cf12b3de90"))
        await session.flush()


async def test_sleepy_device_requires_wake_interval(session: AsyncSession) -> None:
    with pytest.raises(IntegrityError):
        async with session.begin_nested():
            session.add(make_device("aaaaaaaaaa01", power_class="sleepy"))
            await session.flush()

    with pytest.raises(IntegrityError):
        async with session.begin_nested():
            session.add(
                make_device("aaaaaaaaaa02", power_class="sleepy", expected_wake_interval_s=0)
            )
            await session.flush()

    async with session.begin_nested():
        session.add(make_device("aaaaaaaaaa03", power_class="sleepy", expected_wake_interval_s=600))
        await session.flush()


async def test_unknown_power_class_rejected(session: AsyncSession) -> None:
    """Presence is only defined for always_on / sleepy — a third value is undefined."""
    with pytest.raises(IntegrityError):
        async with session.begin_nested():
            session.add(make_device("bbbbbbbbbb01", power_class="hibernating"))
            await session.flush()


async def test_unknown_link_type_accepted(session: AsyncSession) -> None:
    """A link type no agent has invented yet must still enroll (no PG enum)."""
    session.add(make_device("b0b1b2b3b4b5", link_type="satellite", platform_type="rpi5"))
    await session.flush()

    stored = await session.get(Device, "b0b1b2b3b4b5")
    assert stored is not None
    assert stored.link_type == "satellite"


async def test_unknown_deploy_state_accepted(session: AsyncSession) -> None:
    """Same tolerance for the deploy state machine — the ingestor records, never raises."""
    session.add(make_device("cccccccccc01"))
    await session.flush()
    session.add(DeployEvent(device_id="cccccccccc01", state="teleporting"))
    await session.flush()

    states = await session.scalars(
        select(DeployEvent.state).where(DeployEvent.device_id == "cccccccccc01")
    )
    assert list(states) == ["teleporting"]


async def seed_token(session: AsyncSession, **overrides: object) -> EnrollmentToken:
    values: dict[str, object] = {
        "secret_hash": "$argon2id$v=19$m=65536,t=3,p=4$fake",
        "expires_at": dt.datetime.now(dt.UTC) + dt.timedelta(hours=24),
    }
    values.update(overrides)
    token = EnrollmentToken(**values)
    session.add(token)
    await session.flush()
    return token


async def test_token_burn_is_single_use(session: AsyncSession) -> None:
    session.add(make_device("a4cf12b3de90"))
    session.add(make_device("b0b1b2b3b4b5"))
    token = await seed_token(session)

    first = await session.execute(BURN_SQL, {"token_id": token.id, "device_id": "a4cf12b3de90"})
    assert first.rowcount == 1

    second = await session.execute(BURN_SQL, {"token_id": token.id, "device_id": "b0b1b2b3b4b5"})
    assert second.rowcount == 0, "a second burn must never succeed — that is fleet takeover"


async def test_expired_token_cannot_burn(session: AsyncSession) -> None:
    session.add(make_device("a4cf12b3de90"))
    token = await seed_token(session, expires_at=dt.datetime.now(dt.UTC) - dt.timedelta(seconds=1))

    result = await session.execute(BURN_SQL, {"token_id": token.id, "device_id": "a4cf12b3de90"})
    assert result.rowcount == 0


async def test_revoked_token_cannot_burn(session: AsyncSession) -> None:
    session.add(make_device("a4cf12b3de90"))
    token = await seed_token(session, revoked_at=dt.datetime.now(dt.UTC))

    result = await session.execute(BURN_SQL, {"token_id": token.id, "device_id": "a4cf12b3de90"})
    assert result.rowcount == 0


async def test_token_burn_is_atomic_under_concurrency(engine: AsyncEngine) -> None:
    """Two independent connections race the burn; exactly one may win.

    This is the CRITICAL.md property. It needs real committed transactions on
    separate connections — the rolled-back `session` fixture would hide the
    interaction entirely.
    """
    device_a, device_b = "dddddddddd01", "dddddddddd02"

    async with engine.begin() as setup:
        for device_id in (device_a, device_b):
            await setup.execute(
                text(
                    "INSERT INTO devices (device_id, platform_type, link_type, power_class) "
                    "VALUES (:d, 'esp32c6', 'wifi', 'always_on')"
                ),
                {"d": device_id},
            )
        token_id = await setup.scalar(
            text(
                "INSERT INTO enrollment_tokens (secret_hash, expires_at) "
                "VALUES ('$argon2id$v=19$fake', now() + interval '24 hours') RETURNING id"
            )
        )

    async def burn(device_id: str) -> int:
        async with engine.connect() as conn, conn.begin():
            # Default isolation level: the burn's correctness must not depend on
            # anything stricter than READ COMMITTED (see EnrollmentToken docstring).
            isolation = await conn.exec_driver_sql("SHOW transaction_isolation")
            assert isolation.scalar() == "read committed"

            result = await conn.execute(BURN_SQL, {"token_id": token_id, "device_id": device_id})
            return int(result.rowcount)

    try:
        winners = await asyncio.gather(burn(device_a), burn(device_b))
        assert sum(winners) == 1, f"exactly one burn must win, got {winners}"
    finally:
        async with engine.begin() as cleanup:
            await cleanup.execute(
                text("DELETE FROM enrollment_tokens WHERE id = :t"), {"t": token_id}
            )
            await cleanup.execute(
                text("DELETE FROM devices WHERE device_id IN (:a, :b)"),
                {"a": device_a, "b": device_b},
            )


async def test_used_by_device_requires_used_at(session: AsyncSession) -> None:
    """A device recorded against an unburned token means the burn path was bypassed."""
    session.add(make_device("a4cf12b3de90"))
    with pytest.raises(IntegrityError):
        async with session.begin_nested():
            await seed_token(session, used_by_device_id="a4cf12b3de90")


async def test_deploy_events_block_device_delete(session: AsyncSession) -> None:
    """KPI history is kept forever, so a hard delete must be impossible."""
    session.add(make_device("eeeeeeeeee01"))
    await session.flush()
    session.add(
        DeployEvent(
            device_id="eeeeeeeeee01", state="confirmed", is_terminal=True, artifact_version="1.5.0"
        )
    )
    await session.flush()

    with pytest.raises(IntegrityError):
        async with session.begin_nested():
            await session.execute(text("DELETE FROM devices WHERE device_id = 'eeeeeeeeee01'"))


async def test_device_soft_delete_keeps_events(session: AsyncSession) -> None:
    """`decommissioned_at` is the supported removal path; the evidence survives it."""
    device = make_device("eeeeeeeeee02")
    session.add(device)
    await session.flush()
    session.add(DeployEvent(device_id="eeeeeeeeee02", state="confirmed", is_terminal=True))
    await session.flush()

    device.decommissioned_at = dt.datetime.now(dt.UTC)
    await session.flush()

    remaining = await session.scalar(
        select(DeployEvent).where(DeployEvent.device_id == "eeeeeeeeee02")
    )
    assert remaining is not None
    assert device.decommissioned_at is not None


async def test_parent_device_id_self_fk(session: AsyncSession) -> None:
    """The V3 gateway hierarchy is schema-only in v1, but the FK must behave."""
    session.add(make_device("ffffffffff01"))
    await session.flush()
    child = make_device("ffffffffff02", parent_device_id="ffffffffff01")
    session.add(child)
    await session.flush()

    await session.execute(text("DELETE FROM devices WHERE device_id = 'ffffffffff01'"))
    await session.refresh(child)
    assert child.parent_device_id is None


async def test_group_name_unique(session: AsyncSession) -> None:
    session.add(DeviceGroup(name="bench"))
    await session.flush()

    with pytest.raises(IntegrityError):
        async with session.begin_nested():
            session.add(DeviceGroup(name="bench"))
            await session.flush()


async def test_group_delete_nulls_references(session: AsyncSession) -> None:
    """Deleting a group must not cascade into devices or tokens."""
    group = DeviceGroup(name="bench-2")
    session.add(group)
    await session.flush()
    device = make_device("ffffffffff03", group_id=group.id)
    session.add(device)
    token = await seed_token(session, group_id=group.id)

    await session.execute(text("DELETE FROM device_groups WHERE id = :g"), {"g": group.id})
    await session.refresh(device)
    await session.refresh(token)
    assert device.group_id is None
    assert token.group_id is None


def test_secret_hash_is_not_plaintext_column() -> None:
    """Guard against a future "just store the token" regression."""
    forbidden = {"token", "secret", "plaintext", "password"}
    for model in (EnrollmentToken, AdminToken):
        columns = {c.name for c in inspect(model).columns}
        assert "secret_hash" in columns
        assert not (columns & forbidden), f"{model.__tablename__} must not store a plaintext secret"


# ---------------------------------------------------------------------------
# `deploy_events` has exactly one writer (R1-be-2)
# ---------------------------------------------------------------------------

SRC_DIR = Path(__file__).resolve().parent.parent / "src" / "fleetforge"
# The one module allowed to insert into the KPI table, plus the models that define it.
DEPLOY_EVENT_WRITERS = {"deploys.py", "db/models.py"}


def test_only_deploys_py_writes_deploy_events() -> None:
    """`spec/prd.md` → *Retention*: this table is the product's evidence, kept forever.

    Both v1 KPIs are computed over the terminal event of each `(device_id, cmd_id)`
    transaction, so a second writer with its own idea of `is_terminal` is not a bug that
    shows up today — it is a KPI that is quietly wrong in R6. R1-be-4's ingestor path
    adds its `up/status` writer **inside `deploys.py`**, not next to its handler.
    """
    offenders = []
    for path in SRC_DIR.rglob("*.py"):
        relative = path.relative_to(SRC_DIR).as_posix()
        if relative in DEPLOY_EVENT_WRITERS:
            continue
        source = path.read_text(encoding="utf-8")
        code = "\n".join(
            line.split("#", 1)[0]
            for line in source.splitlines()
            if not line.lstrip().startswith("#")
        )
        if "DeployEvent(" in code or "INSERT INTO deploy_events" in code.upper():
            offenders.append(relative)
    assert offenders == [], f"these modules write deploy_events directly: {offenders}"


def test_the_writer_module_exists_and_is_the_one_named() -> None:
    """Non-vacuity: the rule above is worthless if the writer moved and nothing noticed."""
    source = (SRC_DIR / "deploys.py").read_text(encoding="utf-8")
    assert "DeployEvent(" in source

"""Transport-agnostic rollout and deploy policy domain types and exceptions (`A-29`).

Lives outside `fleetforge.api` so core rollout and policy orchestration never imports
FastAPI or router modules. `api/schemas_deploys.py` and `api/schemas_v2.py` re-export
these models directly so OpenAPI contracts remain identical.
"""

from __future__ import annotations

import re
import uuid
from typing import Literal, Protocol

from pydantic import BaseModel

# Bounded version label — rejected, never normalised (`spec/device-protocol.md`).
VERSION_PATTERN = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._+-]{0,63}\Z")

PolicyEvalStatusLiteral = Literal["in_progress", "soaking", "halted", "completed", "skipped"]
RingStatusLiteral = Literal["pending", "in_progress", "soaking", "promoted", "halted", "empty"]


class ActorContext(Protocol):
    """Minimal caller identity needed by rollout/policy orchestration (satisfied by `AuthContext`)."""

    @property
    def token_id(self) -> uuid.UUID: ...

    @property
    def subject(self) -> str: ...


class RolloutValidationError(ValueError):
    """Raised when a rollout request carries an invalid version label or domain parameter."""


class RolloutBrokerError(RuntimeError):
    """Raised when the broker refuses a deploy publish and `best_effort_broker` is False."""


class DeployAccepted(BaseModel):
    """Outcome of staging one firmware artifact onto one device."""

    cmd_id: str
    device_id: str
    version: str
    sha256: str
    size_bytes: int
    apply: str
    reused: bool
    device_online: bool


class PolicyRingStatus(BaseModel):
    """Status of one group cohort ring within a `DeployPolicyEvaluation` (`R11-1`)."""

    ring_index: int
    group_id: uuid.UUID
    status: RingStatusLiteral
    total_devices: int
    confirmed_devices: int
    in_flight_devices: int


class DeployPolicyEvaluation(BaseModel):
    """Result of evaluating or promoting a `DeployPolicy` on a firmware version (`R11-1`)."""

    policy_id: uuid.UUID
    version: str
    status: PolicyEvalStatusLiteral
    halt_reason: str | None
    rings: list[PolicyRingStatus]
    deployed: list[DeployAccepted]

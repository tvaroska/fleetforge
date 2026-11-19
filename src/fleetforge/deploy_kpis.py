"""Delivery-success and fleet-safety KPI aggregation over `deploy_events` (`R6-BE-1`, `A-7`).

* **Delivery success** (`target >= 90%`): `confirmed / total_completed * 100`.
* **Fleet safety** (`target = 100%`): `(confirmed + rolled_back + pre_apply_failed) / total_completed * 100`.
  Server-authored `publish_failed` rows never reached the broker (`counts_in_deploy_kpis` -> False)
  and are excluded alongside `cancelled`. Device-reported pre-apply refusals (where the transaction
  ended `failed` without ever entering `applying`, `rebooting`, or `confirming`) count as delivery
  misses in `total_completed`, while counting as safe in `fleet_safety_pct` because the running slot
  was never switched and the device remained healthy on its prior firmware (`spec/prd.md`).
"""

from __future__ import annotations

import uuid
from dataclasses import dataclass

from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncSession

from fleetforge.db.models import DeployState

KPI_EXCLUDED_TERMINAL_STATES: frozenset[str] = frozenset({DeployState.CANCELLED})

DELIVERY_SUCCESS_TARGET_PCT = 90.0
FLEET_SAFETY_TARGET_PCT = 100.0


def counts_in_deploy_kpis(
    *,
    terminal_state: str,
    has_requested_row: bool,
    terminal_reason: str | None = None,
) -> bool:
    """Return True when a completed transaction belongs in the deploy KPI denominator.

    Excludes transactions with no `requested` row (standalone `rollback` verbs), operator
    `cancelled` transactions, and server-authored `publish_failed` transactions where the
    broker never accepted the `stage` command so the board never saw the deploy (`A-7`).
    """
    if not has_requested_row:
        return False
    if terminal_state in KPI_EXCLUDED_TERMINAL_STATES:
        return False
    if terminal_reason == "publish_failed":
        return False
    return True


_KPI_TRANSACTIONS_ALL_SQL = text(
    """
    WITH requested_txns AS (
        SELECT DISTINCT ON (requested.device_id, requested.cmd_id)
               requested.device_id,
               requested.cmd_id
          FROM deploy_events AS requested
         WHERE requested.state = :requested_state
           AND requested.cmd_id IS NOT NULL
         ORDER BY requested.device_id, requested.cmd_id, requested.at DESC, requested.id DESC
    )
    SELECT r.device_id,
           r.cmd_id,
           (SELECT e.state
              FROM deploy_events AS e
             WHERE e.device_id = r.device_id
               AND e.cmd_id = r.cmd_id
               AND e.is_terminal
             ORDER BY e.at ASC, e.id ASC
             LIMIT 1) AS ended_state,
           (SELECT e.detail ->> 'reason'
              FROM deploy_events AS e
             WHERE e.device_id = r.device_id
               AND e.cmd_id = r.cmd_id
               AND e.is_terminal
             ORDER BY e.at ASC, e.id ASC
             LIMIT 1) AS ended_reason,
           EXISTS (
               SELECT 1
                 FROM deploy_events AS s
                WHERE s.device_id = r.device_id
                  AND s.cmd_id = r.cmd_id
                  AND s.state IN ('applying', 'rebooting', 'confirming')
           ) AS entered_apply
      FROM requested_txns AS r
    """
)

_KPI_TRANSACTIONS_PROJECT_SQL = text(
    """
    WITH requested_txns AS (
        SELECT DISTINCT ON (requested.device_id, requested.cmd_id)
               requested.device_id,
               requested.cmd_id
          FROM deploy_events AS requested
          JOIN devices AS d ON d.device_id = requested.device_id
         WHERE requested.state = :requested_state
           AND requested.cmd_id IS NOT NULL
           AND d.project_id = :project_id
         ORDER BY requested.device_id, requested.cmd_id, requested.at DESC, requested.id DESC
    )
    SELECT r.device_id,
           r.cmd_id,
           (SELECT e.state
              FROM deploy_events AS e
             WHERE e.device_id = r.device_id
               AND e.cmd_id = r.cmd_id
               AND e.is_terminal
             ORDER BY e.at ASC, e.id ASC
             LIMIT 1) AS ended_state,
           (SELECT e.detail ->> 'reason'
              FROM deploy_events AS e
             WHERE e.device_id = r.device_id
               AND e.cmd_id = r.cmd_id
               AND e.is_terminal
             ORDER BY e.at ASC, e.id ASC
             LIMIT 1) AS ended_reason,
           EXISTS (
               SELECT 1
                 FROM deploy_events AS s
                WHERE s.device_id = r.device_id
                  AND s.cmd_id = r.cmd_id
                  AND s.state IN ('applying', 'rebooting', 'confirming')
           ) AS entered_apply
      FROM requested_txns AS r
    """
)


@dataclass(frozen=True, slots=True)
class DeployKpis:
    """R6-BE-1: Delivery success vs. fleet safety over `deploy_events` history."""

    total_completed: int
    confirmed: int
    rolled_back: int
    failed: int
    cancelled: int
    in_flight: int
    delivery_success_pct: float | None
    fleet_safety_pct: float | None
    safety_gap_pct: float | None
    delivery_target_pct: float = DELIVERY_SUCCESS_TARGET_PCT
    safety_target_pct: float = FLEET_SAFETY_TARGET_PCT
    delivery_target_met: bool | None = None
    safety_target_met: bool | None = None


async def deploy_kpis(
    session: AsyncSession,
    *,
    project_id: uuid.UUID | None = None,
) -> DeployKpis:
    """Compute both v1 deploy KPIs over `deploy_events` (optionally scoped to `project_id`)."""
    if project_id is None:
        rows = (
            await session.execute(
                _KPI_TRANSACTIONS_ALL_SQL,
                {"requested_state": DeployState.REQUESTED.value},
            )
        ).all()
    else:
        rows = (
            await session.execute(
                _KPI_TRANSACTIONS_PROJECT_SQL,
                {
                    "requested_state": DeployState.REQUESTED.value,
                    "project_id": project_id,
                },
            )
        ).all()

    confirmed = 0
    rolled_back = 0
    failed = 0
    safe_pre_apply_failed = 0
    cancelled = 0
    in_flight = 0

    for row in rows:
        ended_state = row.ended_state
        if ended_state is None:
            in_flight += 1
            continue
        if not counts_in_deploy_kpis(
            terminal_state=ended_state,
            has_requested_row=True,
            terminal_reason=getattr(row, "ended_reason", None),
        ):
            if ended_state == DeployState.CANCELLED.value:
                cancelled += 1
            continue
        if ended_state == DeployState.CONFIRMED.value:
            confirmed += 1
        elif ended_state == DeployState.ROLLED_BACK.value:
            rolled_back += 1
        else:
            failed += 1
            if not bool(getattr(row, "entered_apply", True)):
                safe_pre_apply_failed += 1

    total_completed = confirmed + rolled_back + failed
    if total_completed == 0:
        return DeployKpis(
            total_completed=0,
            confirmed=0,
            rolled_back=0,
            failed=0,
            cancelled=cancelled,
            in_flight=in_flight,
            delivery_success_pct=None,
            fleet_safety_pct=None,
            safety_gap_pct=None,
            delivery_target_met=None,
            safety_target_met=None,
        )

    safe_total = confirmed + rolled_back + safe_pre_apply_failed
    delivery_pct = round((confirmed / total_completed) * 100.0, 1)
    safety_pct = round((safe_total / total_completed) * 100.0, 1)
    gap_pct = round(((rolled_back + safe_pre_apply_failed) / total_completed) * 100.0, 1)
    return DeployKpis(
        total_completed=total_completed,
        confirmed=confirmed,
        rolled_back=rolled_back,
        failed=failed,
        cancelled=cancelled,
        in_flight=in_flight,
        delivery_success_pct=delivery_pct,
        fleet_safety_pct=safety_pct,
        safety_gap_pct=gap_pct,
        delivery_target_met=(confirmed * 100 >= total_completed * 90),
        safety_target_met=(safe_total == total_completed),
    )

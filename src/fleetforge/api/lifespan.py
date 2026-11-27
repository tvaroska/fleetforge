"""Application lifespan, `ff_events` listener wiring, and rollout reconciliation (`A-11`)."""

from __future__ import annotations

import asyncio
import contextlib
import logging
from collections.abc import AsyncIterator, Callable
from typing import Any

from fastapi import FastAPI
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from fleetforge import rollout
from fleetforge.api.deps import get_command_publisher
from fleetforge.api.eventstream import EventHub, PostgresEventListener
from fleetforge.config import Settings
from fleetforge.db.base import asyncpg_dsn, get_sessionmaker
from fleetforge.events import DeviceEvent, EventType

logger = logging.getLogger(__name__)


def _resolve_sessionmaker(app: FastAPI) -> async_sessionmaker[AsyncSession]:
    override = app.dependency_overrides.get(get_sessionmaker)
    if override is not None:
        result: async_sessionmaker[AsyncSession] = override()
        return result
    return get_sessionmaker()


def _resolve_publisher(app: FastAPI, settings: Settings) -> Any:
    override = app.dependency_overrides.get(get_command_publisher)
    if override is not None:
        return override()
    return get_command_publisher(settings)


async def _handle_device_event(
    event: DeviceEvent,
    *,
    sessionmaker: async_sessionmaker[AsyncSession],
    settings: Settings,
    publisher_getter: Callable[[], Any],
) -> None:
    if event.type == EventType.DEVICE_ANNOUNCE and event.online is not False:
        publisher = publisher_getter()
        async with sessionmaker() as session:
            await rollout.republish_open_requested_on_wake(
                session,
                device_id=event.device_id,
                settings=settings,
                publisher=publisher,
            )
        return
    if (
        event.type == EventType.DEVICE_DEPLOY
        and event.state == "confirmed"
        and event.cmd_id is not None
    ):
        publisher = publisher_getter()
        async with sessionmaker() as session:
            await rollout.advance_project_rollout_on_confirm(
                session,
                cmd_id=event.cmd_id,
                settings=settings,
                publisher=publisher,
                device_id=event.device_id,
            )


async def _rollout_reconcile_loop(
    *,
    sessionmaker: async_sessionmaker[AsyncSession],
    settings: Settings,
    publisher_getter: Callable[[], Any],
) -> None:
    interval = max(0.05, float(settings.rollout_reconcile_interval_s))
    while True:
        await asyncio.sleep(interval)
        try:
            publisher = publisher_getter()
            async with sessionmaker() as session:
                await rollout.reconcile_open_rollouts(
                    session,
                    settings=settings,
                    publisher=publisher,
                )
        except asyncio.CancelledError:
            raise
        except Exception:  # noqa: BLE001
            logger.warning("rollout reconcile tick failed", exc_info=True)


async def _cancel_task(task: asyncio.Task[None] | None) -> None:
    if task is None:
        return
    task.cancel()
    with contextlib.suppress(asyncio.CancelledError):
        await task


def _start_background_tasks(
    app: FastAPI,
    settings: Settings,
    listener_factory: Callable[..., Any] | None,
) -> tuple[asyncio.Task[None], asyncio.Task[None] | None]:
    hub: EventHub = app.state.event_hub
    sessionmaker = _resolve_sessionmaker(app)

    def publisher_getter() -> Any:
        return _resolve_publisher(app, settings)

    async def on_device_event(event: DeviceEvent) -> None:
        await _handle_device_event(
            event,
            sessionmaker=sessionmaker,
            settings=settings,
            publisher_getter=publisher_getter,
        )

    factory = listener_factory or PostgresEventListener
    listener = factory(
        asyncpg_dsn(settings.database_url),
        hub,
        ping_s=settings.events_listener_ping_s,
        on_event=on_device_event,
    )
    app.state.event_listener = listener
    listener_task = asyncio.create_task(listener.run(), name="ff-events-listener")
    reconcile_task: asyncio.Task[None] | None = None
    if float(settings.rollout_reconcile_interval_s) > 0:
        reconcile_task = asyncio.create_task(
            _rollout_reconcile_loop(
                sessionmaker=sessionmaker,
                settings=settings,
                publisher_getter=publisher_getter,
            ),
            name="ff-rollout-reconcile",
        )
    else:
        logger.info("rollout reconcile loop disabled (rollout_reconcile_interval_s <= 0)")
    return listener_task, reconcile_task


def build_lifespan(
    settings: Settings | None,
    listener_factory: Callable[..., Any] | None = None,
) -> Callable[[FastAPI], contextlib.AbstractAsyncContextManager[None]]:
    """Build the per-app lifespan managing `ff_events` LISTEN and rollout reconciliation."""

    @contextlib.asynccontextmanager
    async def lifespan(app: FastAPI) -> AsyncIterator[None]:
        hub: EventHub = app.state.event_hub
        listener_task: asyncio.Task[None] | None = None
        reconcile_task: asyncio.Task[None] | None = None
        if settings is not None:
            listener_task, reconcile_task = _start_background_tasks(app, settings, listener_factory)
        else:
            logger.warning(
                "no settings: the ff_events listener is not running, so /v1/events would "
                "carry no device events. Same cause as the DATABASE_URL warning above."
            )
        try:
            yield
        finally:
            await _cancel_task(reconcile_task)
            await _cancel_task(listener_task)
            hub.close_all()

    return lifespan

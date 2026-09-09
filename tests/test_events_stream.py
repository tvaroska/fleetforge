"""The SSE seam end to end: `EventHub`, the framing, `GET /v1/events`, the listener.

**Read `drive_sse` before adding a test here.** `httpx`'s `ASGITransport` buffers:
it collects every `http.response.body` message and builds the response only after
the app returns, so `client.stream("GET", "/v1/events")` against an endless
generator hangs the entire suite rather than one test. Anything that actually
streams is driven at the ASGI level; only responses that never stream (401, 503) go
through `client_for`.

The listener cases need a real PostgreSQL — they exercise `LISTEN`/`NOTIFY` itself,
which nothing can fake usefully — and they use the shipped producer
(`fleetforge.events.emit`) rather than a hand-rolled `pg_notify`, exactly as
`test_ingestor.py` does.
"""

import asyncio
import contextlib
import json
from collections.abc import AsyncIterator, Callable, Iterator
from typing import Any

import pytest
from fastapi import FastAPI
from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncEngine, AsyncSession

from fleetforge.api import eventstream
from fleetforge.api.eventstream import (
    LISTENER_APPLICATION_NAME,
    EventHub,
    HubFull,
    PostgresEventListener,
    sse_frames,
)
from fleetforge.clock import now_utc
from fleetforge.config import get_settings
from fleetforge.db.base import asyncpg_dsn
from fleetforge.events import DeviceEvent, EventType, emit
from tests.conftest import (
    TEST_DB_NAME,
    client_for,
    database_url_for,
    login_admin,
    settings_for_tests,
)

DEVICE_ID = "a4cf12b3de91"


def an_event(**overrides: Any) -> DeviceEvent:
    values: dict[str, Any] = {
        "type": EventType.DEVICE_HEARTBEAT,
        "device_id": DEVICE_ID,
        "at": now_utc(),
        "online": True,
        "fw_version": "1.4.2",
    }
    values.update(overrides)
    return DeviceEvent(**values)


# ---------------------------------------------------------------------------
# EventHub — the fan-out
# ---------------------------------------------------------------------------


async def test_every_subscriber_receives_every_event() -> None:
    """The fan-out itself: one NOTIFY, N dashboards."""
    hub = EventHub(queue_size=4, max_subscribers=4)
    with hub.subscribe() as first, hub.subscribe() as second:
        hub.publish("payload")
        assert first.queue.get_nowait() == "payload"
        assert second.queue.get_nowait() == "payload"


async def test_a_client_that_stops_reading_is_dropped_and_the_others_are_not() -> None:
    """One stuck browser tab must not stall the fleet view, and must not buffer either."""
    hub = EventHub(queue_size=2, max_subscribers=4)
    with hub.subscribe() as stuck, hub.subscribe() as healthy:
        for index in range(5):
            hub.publish(f"payload-{index}")
            # The healthy client keeps up.
            healthy.queue.get_nowait()

        assert hub.subscriber_count == 1, "the stuck subscriber was dropped, the healthy one kept"
        # Its queue was drained and ended: a sentinel, not a backlog.
        assert stuck.queue.get_nowait() is None
        assert stuck.queue.empty()
        # And it keeps working for the client that is reading.
        hub.publish("after")
        assert healthy.queue.get_nowait() == "after"


async def test_publish_never_raises_with_no_subscribers() -> None:
    """`publish()` runs inside asyncpg's callback, where an exception is swallowed."""
    EventHub(queue_size=1, max_subscribers=1).publish("payload")


async def test_acquire_is_capped_and_release_frees_the_slot() -> None:
    hub = EventHub(queue_size=1, max_subscribers=1)
    first = hub.acquire()
    with pytest.raises(HubFull):
        hub.acquire()

    hub.release(first)
    hub.release(first)  # idempotent: an overflow may already have dropped it
    assert hub.subscriber_count == 0
    hub.release(hub.acquire())


async def test_close_all_ends_every_stream() -> None:
    """How a listener reconnect is reported: end everything, let the clients resync."""
    hub = EventHub(queue_size=4, max_subscribers=4)
    with hub.subscribe() as first, hub.subscribe() as second:
        hub.publish("stale")
        hub.close_all()
        assert first.queue.get_nowait() is None, "the backlog is dropped, not delivered"
        assert second.queue.get_nowait() is None


# ---------------------------------------------------------------------------
# SSE framing
# ---------------------------------------------------------------------------


async def take(frames: AsyncIterator[bytes], count: int, timeout: float = 5.0) -> list[bytes]:
    """The first `count` frames, then close the generator."""
    collected: list[bytes] = []

    async def pump() -> None:
        async for frame in frames:
            collected.append(frame)
            if len(collected) >= count:
                return

    try:
        await asyncio.wait_for(pump(), timeout)
    finally:
        await frames.aclose()
    return collected


async def drain(frames: AsyncIterator[bytes], timeout: float = 5.0) -> list[bytes]:
    """Every frame until the stream ends by itself."""
    collected: list[bytes] = []

    async def pump() -> None:
        async for frame in frames:
            collected.append(frame)

    await asyncio.wait_for(pump(), timeout)
    return collected


async def test_the_first_frame_flushes_headers_and_sets_the_retry() -> None:
    """A proxy or client waiting on the first byte must never sit on an empty socket."""
    queue: asyncio.Queue[str | None] = asyncio.Queue()
    frames = sse_frames(queue, keepalive_s=5, max_stream_s=5)
    assert await take(frames, 1) == [b": connected\nretry: 2000\n\n"]


async def test_a_data_frame_is_one_line_and_carries_the_payload_verbatim() -> None:
    payload = an_event().model_dump_json()
    queue: asyncio.Queue[str | None] = asyncio.Queue()
    queue.put_nowait(payload)

    frames = sse_frames(queue, keepalive_s=5, max_stream_s=5)
    data = (await take(frames, 2))[1]

    assert data == f"data: {payload}\n\n".encode()
    assert b"\n" not in data[: -len(b"\n\n")], "a newline inside a frame is a forged second event"
    assert DeviceEvent.model_validate_json(data[len(b"data: ") : -2]).device_id == DEVICE_ID


async def test_an_idle_stream_emits_keepalive_comments() -> None:
    queue: asyncio.Queue[str | None] = asyncio.Queue()
    frames = sse_frames(queue, keepalive_s=0.05, max_stream_s=5)
    assert await take(frames, 3) == [
        b": connected\nretry: 2000\n\n",
        b": keepalive\n\n",
        b": keepalive\n\n",
    ]


async def test_the_stream_ends_at_max_stream_s() -> None:
    """The bound on how long a revoked admin token can keep reading (config.sse_max_stream_s)."""
    queue: asyncio.Queue[str | None] = asyncio.Queue()
    frames = sse_frames(queue, keepalive_s=5, max_stream_s=0.1)
    assert await drain(frames) == [b": connected\nretry: 2000\n\n"]


async def test_the_sentinel_ends_the_stream() -> None:
    queue: asyncio.Queue[str | None] = asyncio.Queue()
    queue.put_nowait(None)
    frames = sse_frames(queue, keepalive_s=5, max_stream_s=5)
    assert await drain(frames) == [b": connected\nretry: 2000\n\n"]


# ---------------------------------------------------------------------------
# What the listener refuses to forward
# ---------------------------------------------------------------------------


def a_listener(hub: EventHub) -> PostgresEventListener:
    """A listener that is never started — only its notify callback is exercised."""
    return PostgresEventListener("postgresql://unused/unused", hub, ping_s=1.0)


@pytest.mark.parametrize(
    "payload",
    [
        pytest.param('{"v":1,"type":"device.heartbeat","device_id":"' + DEVICE_ID + '",\n"x":1}'),
        pytest.param('{"v":1}\r\ndata: {"forged":true}'),
    ],
)
async def test_a_payload_that_cannot_be_framed_safely_is_dropped(payload: str) -> None:
    """`fw_version` comes off the wire from a board; a raw newline would forge a frame."""
    hub = EventHub(queue_size=4, max_subscribers=4)
    with hub.subscribe() as subscription:
        a_listener(hub)._on_notify(None, 1, "ff_events", payload)
        assert subscription.queue.empty()


async def test_an_unparseable_payload_is_dropped() -> None:
    hub = EventHub(queue_size=4, max_subscribers=4)
    with hub.subscribe() as subscription:
        a_listener(hub)._on_notify(None, 1, "ff_events", '{"not":"an event"}')
        assert subscription.queue.empty()


async def test_an_unknown_extra_field_is_forwarded_verbatim() -> None:
    """Additive evolution: re-serializing would strip what a newer ingestor added."""
    payload = json.dumps(
        {
            "v": 1,
            "type": "device.heartbeat",
            "device_id": DEVICE_ID,
            "at": "2026-09-08T10:00:00+00:00",
            "online": True,
            "from_a_newer_ingestor": "keep me",
        }
    )
    hub = EventHub(queue_size=4, max_subscribers=4)
    with hub.subscribe() as subscription:
        a_listener(hub)._on_notify(None, 1, "ff_events", payload)
        assert subscription.queue.get_nowait() == payload


# ---------------------------------------------------------------------------
# GET /v1/events
# ---------------------------------------------------------------------------


async def drive_sse(
    app: FastAPI,
    *,
    headers: dict[str, str],
    stop_after: int = 1,
    timeout: float = 5.0,
    on_attach: Callable[[], None] | None = None,
) -> tuple[int, dict[bytes, bytes], list[bytes]]:
    """Drive `/v1/events` at the ASGI level, disconnecting after `stop_after` data frames.

    `httpx`'s `ASGITransport` BUFFERS — it joins every body chunk and returns only
    once the app has finished — so `client.stream(...)` against an endless SSE
    generator hangs the whole suite. The scope deliberately declares no
    `spec_version` (so it defaults to "2.0"), which is what makes Starlette take its
    `listen_for_disconnect` path and **cancel** the body generator; uvicorn reports
    2.3 and behaves the same way. That is the path the endpoint's `finally:` exists
    for.

    `on_attach` fires once the response has started, i.e. once the subscription is
    in the hub — publishing before that would go nowhere.
    """
    scope: dict[str, Any] = {
        "type": "http",
        "asgi": {"version": "3.0"},
        "http_version": "1.1",
        "method": "GET",
        "scheme": "http",
        "path": "/v1/events",
        "raw_path": b"/v1/events",
        "query_string": b"",
        "root_path": "",
        "headers": [(key.lower().encode(), value.encode()) for key, value in headers.items()],
        "client": ("127.0.0.1", 1234),
        "server": ("testserver", 80),
    }
    chunks: list[bytes] = []
    started: dict[str, Any] = {}
    disconnect = asyncio.Event()

    async def receive() -> dict[str, Any]:
        await disconnect.wait()
        return {"type": "http.disconnect"}

    async def send(message: dict[str, Any]) -> None:
        if message["type"] == "http.response.start":
            started.update(message)
            if on_attach is not None:
                on_attach()
        elif message["type"] == "http.response.body" and message.get("body"):
            chunks.append(message["body"])
            if sum(chunk.startswith(b"data:") for chunk in chunks) >= stop_after:
                disconnect.set()

    await asyncio.wait_for(app(scope, receive, send), timeout)
    return int(started["status"]), dict(started.get("headers", [])), chunks


@pytest.fixture
def fast_stream(admin_app: FastAPI) -> FastAPI:
    """`admin_app` with a stream short enough to run in a test."""
    settings = settings_for_tests(sse_keepalive_s=0.05, sse_max_stream_s=0.4)
    admin_app.dependency_overrides[get_settings] = lambda: settings
    return admin_app


def hub_of(app: FastAPI) -> EventHub:
    hub: EventHub = app.state.event_hub
    return hub


async def test_no_credential_is_401_before_any_stream_starts(admin_app: FastAPI) -> None:
    """The whole point of the route being admin-protected — and it must not hang."""
    async with client_for(admin_app) as client:
        response = await client.get("/v1/events")

    assert response.status_code == 401
    assert "text/event-stream" not in response.headers["content-type"]
    assert hub_of(admin_app).subscriber_count == 0, "nothing was acquired"


async def test_a_bad_token_is_401(admin_app: FastAPI) -> None:
    async with client_for(admin_app) as client:
        response = await client.get("/v1/events", headers={"Authorization": "Bearer ffa_nope.nope"})
    assert response.status_code == 401


async def test_no_cors_headers_on_the_stream(admin_app: FastAPI) -> None:
    """The one-origin invariant holds here too — `EventSource` is same-origin by design."""
    async with client_for(admin_app) as client:
        response = await client.get("/v1/events", headers={"Origin": "https://evil.example.com"})
    assert "access-control-allow-origin" not in {key.lower() for key in response.headers}


async def test_the_bearer_transport_streams(fast_stream: FastAPI) -> None:
    token = await login_admin(fast_stream)
    payload = an_event().model_dump_json()

    status, headers, chunks = await drive_sse(
        fast_stream,
        headers={"Authorization": f"Bearer {token}"},
        on_attach=lambda: hub_of(fast_stream).publish(payload),
    )

    assert status == 200
    assert headers[b"content-type"] == b"text/event-stream; charset=utf-8"
    assert headers[b"cache-control"] == b"no-store"
    assert headers[b"x-accel-buffering"] == b"no"
    assert chunks[0] == b": connected\nretry: 2000\n\n"
    assert chunks[-1] == f"data: {payload}\n\n".encode()
    assert DeviceEvent.model_validate_json(payload).device_id == DEVICE_ID


async def test_the_cookie_transport_streams(fast_stream: FastAPI) -> None:
    """Browser `EventSource` cannot set a header — R0-be-1's second transport is load-bearing."""
    token = await login_admin(fast_stream)
    payload = an_event(type=EventType.DEVICE_PRESENCE).model_dump_json()

    status, _, chunks = await drive_sse(
        fast_stream,
        headers={"Cookie": f"ff_session={token}"},
        on_attach=lambda: hub_of(fast_stream).publish(payload),
    )

    assert status == 200
    assert chunks[-1] == f"data: {payload}\n\n".encode()


async def test_the_subscription_is_released_when_the_client_disconnects(
    fast_stream: FastAPI,
) -> None:
    """The leak test: Starlette cancels the generator, so only its `finally` can release."""
    token = await login_admin(fast_stream)
    hub = hub_of(fast_stream)

    for _ in range(3):
        await drive_sse(
            fast_stream,
            headers={"Authorization": f"Bearer {token}"},
            on_attach=lambda: hub.publish(an_event().model_dump_json()),
        )
        assert hub.subscriber_count == 0


async def test_too_many_streams_is_a_clean_503(admin_app: FastAPI) -> None:
    """Acquiring before the response is what makes this a 503 and not a truncated 200."""
    token = await login_admin(admin_app)
    admin_app.state.event_hub = EventHub(queue_size=2, max_subscribers=1)
    occupied = hub_of(admin_app).acquire()

    async with client_for(admin_app) as client:
        response = await client.get("/v1/events", headers={"Authorization": f"Bearer {token}"})

    assert response.status_code == 503
    assert response.headers["retry-after"] == "5"
    assert hub_of(admin_app).subscriber_count == 1, "the refused client acquired nothing"
    hub_of(admin_app).release(occupied)


async def test_both_new_routes_are_in_the_versioned_schema(admin_app: FastAPI) -> None:
    async with client_for(admin_app) as client:
        schema = (await client.get("/v1/openapi.json")).json()
    assert "/v1/events" in schema["paths"]
    assert "/v1/devices" in schema["paths"]
    assert all(path.startswith("/v1") for path in schema["paths"])


# ---------------------------------------------------------------------------
# PostgresEventListener — against the real database
# ---------------------------------------------------------------------------


@contextlib.asynccontextmanager
async def running_listener(
    hub: EventHub, *, ping_s: float = 0.5
) -> AsyncIterator[PostgresEventListener]:
    """A started listener, always cancelled — a leaked task pollutes the session loop."""
    listener = PostgresEventListener(
        asyncpg_dsn(database_url_for(TEST_DB_NAME)), hub, ping_s=ping_s
    )
    task = asyncio.create_task(listener.run(), name="test-ff-events-listener")
    try:
        await asyncio.wait_for(listener.ready.wait(), timeout=10)
        yield listener
    finally:
        task.cancel()
        with contextlib.suppress(asyncio.CancelledError):
            await task


async def notify(engine: AsyncEngine, event: DeviceEvent) -> str:
    """Emit through the SHIPPED producer, so this exercises `events.emit`, not a copy."""
    async with AsyncSession(engine) as session:
        await emit(session, event)
        await session.commit()
    return event.model_dump_json()


@pytest.fixture
def subscribed() -> Iterator[tuple[EventHub, Any]]:
    hub = EventHub(queue_size=8, max_subscribers=4)
    with hub.subscribe() as subscription:
        yield hub, subscription


async def test_a_committed_notify_reaches_a_subscriber(
    engine: AsyncEngine, subscribed: tuple[EventHub, Any]
) -> None:
    """The property the whole task rests on, over a real `LISTEN` connection."""
    hub, subscription = subscribed
    async with running_listener(hub):
        payload = await notify(engine, an_event())
        assert await asyncio.wait_for(subscription.queue.get(), timeout=5) == payload


async def test_the_listener_is_visible_in_pg_stat_activity(
    engine: AsyncEngine, subscribed: tuple[EventHub, Any]
) -> None:
    """How an operator answers "is anything listening?" without reading code."""
    hub, _ = subscribed
    async with running_listener(hub), engine.connect() as connection:
        found = await connection.scalar(
            text(
                "SELECT count(*) FROM pg_stat_activity "
                "WHERE application_name = :name AND datname = :db"
            ),
            {"name": LISTENER_APPLICATION_NAME, "db": TEST_DB_NAME},
        )
    assert found == 1


async def test_a_dropped_connection_ends_every_stream_and_then_recovers(
    engine: AsyncEngine,
    subscribed: tuple[EventHub, Any],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A database blip must self-heal, and every client must be told to resync."""
    monkeypatch.setattr(eventstream, "RECONNECT_INITIAL_DELAY", 0.05)
    hub, subscription = subscribed

    async with running_listener(hub, ping_s=0.2) as listener:
        async with engine.begin() as connection:
            await connection.execute(
                text(
                    "SELECT pg_terminate_backend(pid) FROM pg_stat_activity "
                    "WHERE application_name = :name AND datname = :db"
                ),
                {"name": LISTENER_APPLICATION_NAME, "db": TEST_DB_NAME},
            )

        # The sentinel is emitted after `ready.clear()`, so receiving it proves the
        # listener noticed and that every attached client was told to resync.
        assert await asyncio.wait_for(subscription.queue.get(), timeout=10) is None
        await asyncio.wait_for(listener.ready.wait(), timeout=10)

        payload = await notify(engine, an_event(type=EventType.DEVICE_ANNOUNCE))
        assert await asyncio.wait_for(subscription.queue.get(), timeout=5) == payload

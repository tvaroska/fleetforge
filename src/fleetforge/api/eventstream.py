"""The consumer half of the `ff_events` seam: `LISTEN` → in-process fan-out → SSE.

`fleetforge.events` is the producer half (the ingestor and `POST /v1/enroll` both
call `emit()`); this module owns everything between PostgreSQL's `NOTIFY` and the
bytes on an operator's socket. `EVENTS_CHANNEL` and `DeviceEvent` are **imported**
from there and never restated — a channel name spelled in two files is a silently
empty SSE stream with nothing failing loudly.

```
Postgres ── NOTIFY ──►  PostgresEventListener   (one dedicated asyncpg conn per API
                              │                  process; `LISTEN` only delivers to a
                              │                  backend between transactions, so it
                              │                  can never be a pooled connection)
                              ▼
                        EventHub.publish()       (SYNC — it runs inside asyncpg's
                              │                   notify callback)
                              ▼
                     per-client asyncio.Queue ──► sse_frames() ──► GET /v1/events
```

Nothing here imports FastAPI, so all of it is testable without an app.

Three rules that are the answer to a specific failure, and are easy to undo by
accident:

* **A slow client is disconnected, not buffered.** Queues are bounded; on overflow
  the queue is drained and a sentinel ends that one stream. Dropping individual
  events instead would leave the client silently showing a stale fleet, and
  unbounded buffering is a memory leak in a 256 M container. `EventSource`
  reconnects and re-reads `GET /v1/devices`, so the client self-heals.
* **A listener reconnect ends every stream** (`close_all`), for the same reason: the
  hub cannot know what was missed while the connection was down. That is also why
  there is no "resync" event type.
* **The payload is validated and then forwarded verbatim.** `fw_version` comes off
  the wire from a board and SSE framing is newline-delimited, so a payload carrying
  a raw newline would let a device inject a forged event into the operator's stream.
  Re-serializing instead would strip fields a newer ingestor adds, breaking the
  additive-evolution rule the whole protocol runs on.
"""

import asyncio
import contextlib
import logging
from collections.abc import AsyncIterator, Iterator
from dataclasses import dataclass, field
from typing import Any

import asyncpg
from pydantic import ValidationError

from fleetforge.events import EVENTS_CHANNEL, NOTIFY_PAYLOAD_LIMIT, DeviceEvent

logger = logging.getLogger(__name__)

# The same shape and the same numbers as `ingestor/main.py::run`, deliberately: an
# operator reading both logs should not have to learn two reconnect idioms.
RECONNECT_INITIAL_DELAY = 1.0
RECONNECT_MAX_DELAY = 30.0

# How `pg_stat_activity` answers "is anything actually listening?" without reading
# code, and how the reconnect test finds exactly the right backend to terminate.
LISTENER_APPLICATION_NAME = "fleetforge-events"


class HubFull(Exception):
    """Raised by `EventHub.acquire()` when `max_subscribers` streams are already open."""


@dataclass(slots=True, eq=False)
class Subscription:
    """One attached SSE client. Identity-hashed: two clients are never "equal"."""

    queue: asyncio.Queue[str | None] = field(repr=False)


class EventHub:
    """In-process fan-out from one `LISTEN` connection to N SSE clients.

    Created per app in `create_app()` (never module-level, so no state leaks between
    test apps) and reached through `api/deps.py::event_hub`. R1's deploy-status
    events and R3's telemetry publish into this same hub rather than inventing a
    second one.
    """

    def __init__(self, *, queue_size: int, max_subscribers: int) -> None:
        self._queue_size = queue_size
        self._max_subscribers = max_subscribers
        self._subscribers: set[Subscription] = set()

    @property
    def subscriber_count(self) -> int:
        """How many streams are attached right now."""
        return len(self._subscribers)

    def acquire(self) -> Subscription:
        """Attach a subscriber, or raise `HubFull`.

        Called by the endpoint **before** it returns a `StreamingResponse`: acquiring
        inside the body generator would turn "too many clients" into a 500 mid-stream,
        because the response has already started by then.
        """
        if len(self._subscribers) >= self._max_subscribers:
            raise HubFull
        subscription = Subscription(queue=asyncio.Queue(maxsize=self._queue_size))
        self._subscribers.add(subscription)
        return subscription

    def release(self, subscription: Subscription) -> None:
        """Detach a subscriber. Idempotent — `publish()` may already have dropped it."""
        self._subscribers.discard(subscription)

    @contextlib.contextmanager
    def subscribe(self) -> Iterator[Subscription]:
        """`acquire()`/`release()` as a context manager, for callers that are not the endpoint."""
        subscription = self.acquire()
        try:
            yield subscription
        finally:
            self.release(subscription)

    def publish(self, payload: str) -> None:
        """Fan `payload` out to every attached client. **Never raises.**

        This runs inside asyncpg's notify callback, which is synchronous and whose
        exceptions asyncpg swallows — so every operation here is total by
        construction: a copy of the subscriber set, `put_nowait` guarded by the one
        `QueueFull` handler, and `discard`.
        """
        for subscription in list(self._subscribers):
            try:
                subscription.queue.put_nowait(payload)
            except asyncio.QueueFull:
                # This client stopped reading. Drop it rather than grow with it, and
                # tell it so: the sentinel ends the stream, the browser reconnects
                # and re-reads `GET /v1/devices`.
                logger.warning("sse client is not keeping up; closing its stream")
                self._subscribers.discard(subscription)
                _drain(subscription.queue)
                with contextlib.suppress(asyncio.QueueFull):
                    subscription.queue.put_nowait(None)

    def close_all(self) -> None:
        """End every open stream, leaving the clients to reconnect and resync.

        How a listener reconnect is reported. The subscriptions stay in the set until
        each stream's own `finally` releases it, so `subscriber_count` keeps counting
        sockets that are actually open.
        """
        for subscription in list(self._subscribers):
            _drain(subscription.queue)
            with contextlib.suppress(asyncio.QueueFull):
                subscription.queue.put_nowait(None)


def _drain(queue: asyncio.Queue[str | None]) -> None:
    """Empty `queue` without awaiting. Total, like everything `publish()` touches."""
    while True:
        try:
            queue.get_nowait()
        except asyncio.QueueEmpty:
            return


async def sse_frames(
    queue: asyncio.Queue[str | None], *, keepalive_s: float, max_stream_s: float
) -> AsyncIterator[bytes]:
    """Yield `text/event-stream` frames from `queue` until it ends or time runs out.

    The leading comment frame flushes the headers immediately, so a proxy or a client
    waiting on the first byte never sits on an empty socket. `retry: 2000` is the
    browser's reconnect delay and matches `spec/prd.md` → *Timing* ("≤ 2 s"), so a
    reconnect can never blow that number.

    Deliberately **no `id:` line**: PostgreSQL `NOTIFY` has no backlog, so
    `Last-Event-ID` resumption is impossible and advertising it would be a lie. And
    deliberately **no `event:` name**: `EventSource.onmessage` fires only for unnamed
    events, so naming them would force every client to pre-register every type — the
    opposite of "a reader ignores types it does not know" (`events.py::EventType`).
    """
    yield b": connected\nretry: 2000\n\n"
    loop = asyncio.get_running_loop()
    deadline = loop.time() + max_stream_s
    while True:
        remaining = deadline - loop.time()
        if remaining <= 0:
            # Auth was checked once, at connect. The cap is what keeps a revoked
            # token from reading events forever — the client reconnects and
            # re-authenticates. See config.sse_max_stream_s.
            return
        try:
            payload = await asyncio.wait_for(queue.get(), timeout=min(keepalive_s, remaining))
        except TimeoutError:
            # The wait is capped by whichever comes first. Only a *quiet interval*
            # deserves a keepalive; a timeout that is the deadline falls through to
            # the check above and ends the stream, rather than trailing a comment
            # frame the client will never read.
            if deadline - loop.time() > 0:
                yield b": keepalive\n\n"
            continue
        if payload is None:
            return  # sentinel: this client overflowed, or the listener dropped
        yield f"data: {payload}\n\n".encode()


class PostgresEventListener:
    """One dedicated asyncpg connection per API process, `LISTEN`ing on `ff_events`.

    **Not a pooled connection, and never inside a transaction.** PostgreSQL delivers
    a `NotificationResponse` only to a backend that is between transactions, and
    `get_engine()`'s `pool_pre_ping`/recycle would drop the registration with nothing
    in the log — the symptom is an SSE stream that connects and stays empty forever.
    This is the one documented exception to "`get_sessionmaker()` is the only door
    into the database from the API" (`db/base.py`).
    """

    def __init__(self, dsn: str, hub: EventHub, *, ping_s: float) -> None:
        self._dsn = dsn
        self._hub = hub
        self._ping_s = ping_s
        # Set while connected and registered. Tests wait on it; nothing in the
        # request path does — `/v1/readyz` deliberately ignores the listener, because
        # a reconnecting listener must not restart the container mid-outage.
        self.ready = asyncio.Event()

    async def run(self) -> None:
        """Listen forever, reconnecting with capped exponential backoff.

        Cancelled by the app lifespan. `asyncio.CancelledError` is a `BaseException`
        and neither caught family includes it, so shutdown is prompt.
        """
        delay = RECONNECT_INITIAL_DELAY
        while True:
            try:
                await self._listen_once()
                logger.warning("ff_events listener closed; reconnecting")
            # `InterfaceError` alongside the two obvious families: asyncpg raises it
            # for "connection is closed", which is exactly what the keepalive
            # `SELECT 1` hits when the socket died between the two. Letting it escape
            # would kill this task for the life of the process, and the symptom is
            # the one this module exists to prevent — a stream that connects and
            # stays empty forever, with nothing unhealthy anywhere.
            except (asyncpg.PostgresError, asyncpg.InterfaceError, OSError) as exc:
                logger.warning("ff_events listener error (%s); reconnecting in %.0fs", exc, delay)
            else:
                delay = RECONNECT_INITIAL_DELAY
            finally:
                self.ready.clear()
                # We cannot know what was missed while the connection was down, so
                # every client is ended and resyncs against `GET /v1/devices`.
                self._hub.close_all()
            await asyncio.sleep(delay)
            delay = min(delay * 2, RECONNECT_MAX_DELAY)

    async def _listen_once(self) -> None:
        """Connect, register, and stay until the connection dies."""
        connection = await asyncpg.connect(
            self._dsn, server_settings={"application_name": LISTENER_APPLICATION_NAME}
        )
        try:
            terminated = asyncio.Event()
            connection.add_termination_listener(lambda _connection: terminated.set())
            await connection.add_listener(EVENTS_CHANNEL, self._on_notify)
            self.ready.set()
            logger.info("listening on %s", EVENTS_CHANNEL)
            while not terminated.is_set():
                try:
                    await asyncio.wait_for(terminated.wait(), timeout=self._ping_s)
                except TimeoutError:
                    # A silently dead TCP socket fires no termination callback. One
                    # cheap statement per `ping_s` is what turns that into a
                    # reconnect instead of a stream that is up and permanently empty.
                    await connection.execute("SELECT 1")
        finally:
            # `terminate()`, not `await close()`: closing a dead connection can hang
            # or raise, and this runs on the shutdown path.
            connection.terminate()

    def _on_notify(self, _connection: Any, _pid: int, _channel: str, payload: str) -> None:
        """asyncpg's notify callback. Synchronous, and it must never raise.

        The payload body is never logged — same rule as the ingestor's message loop.
        """
        if len(payload) > NOTIFY_PAYLOAD_LIMIT or "\n" in payload or "\r" in payload:
            logger.warning("dropping an ff_events payload that cannot be framed safely")
            return
        try:
            DeviceEvent.model_validate_json(payload)
        except ValidationError:
            logger.warning("dropping an unparseable ff_events payload")
            return
        # Validated, then forwarded as-is: re-serializing would strip any field a
        # newer ingestor added, and additive evolution is the protocol's whole rule.
        self._hub.publish(payload)

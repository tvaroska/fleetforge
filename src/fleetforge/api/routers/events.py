"""`GET /v1/events` — the live device event stream (Server-Sent Events). **CRITICAL.**

The read side of `fleetforge.events`: the ingestor and `POST /v1/enroll` `NOTIFY` on
`ff_events`, one `PostgresEventListener` per API process fans out into an `EventHub`
(`api/eventstream.py`), and this endpoint turns one subscriber's queue into frames.

Three properties, each of which is a decision and not an accident:

* **The event is a hint, never the record.** A client re-reads `GET /v1/devices` on
  every frame, because presence is derived and a sleepy device goes offline with no
  event at all (`fleetforge.presence`). That is also why a dropped or overflowed
  stream simply ends: reconnect + re-read is the resync path, so no replay, no
  `Last-Event-ID`, no "resync" event type.
* **One credential, two transports** (`DECISIONS.md` R0-be-1). Browser `EventSource`
  cannot set an `Authorization` header at all — the dashboard is authenticated by
  the same-origin `ff_session` cookie, curl and the CLI by the bearer header, and
  both land on the one `require_admin`. **A token in the query string was rejected:**
  nginx's access log format records `$request`.
* **Auth is checked once, at connect**, so the stream is capped at
  `settings.sse_max_stream_s` (15 min). Instant revocation is the reason JWT was
  rejected; an unbounded stream would quietly outlive a revoked token. The cap is
  deliberately under nginx's `proxy_read_timeout 3600s`.

`frontend/nginx.conf`'s `proxy_buffering off` block on `location /v1/` is what makes
this work through the dashboard's origin; `X-Accel-Buffering: no` below is for the
next reverse proxy, which will not be that one.
"""

import logging
from collections.abc import AsyncIterator

from fastapi import APIRouter, Depends, HTTPException, Request, status
from fastapi.responses import StreamingResponse

from fleetforge.api.deps import (
    AdminDep,
    SettingsDep,
    bearer_scheme,
    cookie_scheme,
    event_hub,
)
from fleetforge.api.eventstream import HubFull, sse_frames

logger = logging.getLogger(__name__)

router = APIRouter(
    prefix="/v1",
    tags=["events"],
    dependencies=[Depends(bearer_scheme), Depends(cookie_scheme)],
)

TOO_MANY_STREAMS = HTTPException(
    status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
    detail="too many event stream clients",
    headers={"Retry-After": "5"},
)


@router.get(
    "/events",
    summary="Live device events (Server-Sent Events)",
    response_class=StreamingResponse,
    responses={
        200: {"content": {"text/event-stream": {}}, "description": "An SSE stream"},
        503: {"description": "Too many concurrent event stream clients"},
    },
)
async def events(request: Request, admin: AdminDep, settings: SettingsDep) -> StreamingResponse:
    """Stream `ff_events` as SSE until the client goes away or the cap is reached.

    Frames are `: connected` + `retry: 2000` on connect, then one unnamed
    `data: {DeviceEvent JSON}` per event and a `: keepalive` comment every
    `sse_keepalive_s` of quiet. Types are additive — **ignore a `type` you do not
    know** — and on any frame the correct reaction is to re-read `GET /v1/devices`.

    **Do not press "Try it out" in `/v1/docs`:** this never returns, so Swagger UI
    will sit spinning for `sse_max_stream_s`. Use `curl -N`.
    """
    hub = event_hub(request)
    try:
        # Acquired here rather than inside the generator: by the time the body runs,
        # the response has started and "too many clients" could only be a truncated
        # 200 instead of a clean 503.
        subscription = hub.acquire()
    except HubFull:
        logger.warning("refusing an sse client: %d streams already open", hub.subscriber_count)
        raise TOO_MANY_STREAMS from None

    async def body() -> AsyncIterator[bytes]:
        logger.info("sse client attached (%d open)", hub.subscriber_count)
        try:
            async for frame in sse_frames(
                subscription.queue,
                keepalive_s=settings.sse_keepalive_s,
                max_stream_s=settings.sse_max_stream_s,
            ):
                yield frame
        finally:
            # Starlette *cancels* this generator when the client disconnects (for
            # ASGI spec_version < 2.4, which is what uvicorn reports), so it is never
            # returned from normally. Releasing anywhere but here leaks a subscriber
            # per disconnect, and the symptom is a 503 after `sse_max_clients` page
            # reloads.
            hub.release(subscription)
            logger.info("sse client detached (%d open)", hub.subscriber_count)

    return StreamingResponse(
        body(),
        media_type="text/event-stream",
        headers={"Cache-Control": "no-store", "X-Accel-Buffering": "no"},
    )

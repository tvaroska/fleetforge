"""The ingestor's liveness file, with **no broker and no database** (S0-infra-8).

The healthcheck reads only the heartbeat file's mtime, so what these tests prove is
what that mtime means: "connected and consuming". Three properties, each a way the
probe has lied or could lie:

* **an idle session stays fresh** — the original bug: the file was touched only when
  a device spoke, so a zero-device stack sat unhealthy for 27 hours;
* **a dropped session goes stale** — the timer must not outlive the connection;
* **a wedged consumer goes stale** — a timer alone stays green over a hung write.
"""

import asyncio
import os
import time
from collections.abc import AsyncIterator
from pathlib import Path
from typing import Any

import pytest

from fleetforge.ingestor import main as ingestor

TICK = 0.01


class FakeMessage:
    def __init__(self) -> None:
        self.topic = type("Topic", (), {"value": "ff/v1/d/0123456789ab/up/hb"})()
        self.payload = b"{}"
        self.qos = 1
        self.retain = False


class FakeClient:
    """`aiomqtt.Client` as `run_once` uses it; `drop()` ends the session like a dead broker."""

    def __init__(self, **_kwargs: Any) -> None:
        self.inbox: asyncio.Queue[FakeMessage | None] = asyncio.Queue()
        self.messages = self._drain()

    async def _drain(self) -> AsyncIterator[FakeMessage]:
        while (message := await self.inbox.get()) is not None:
            yield message

    def drop(self) -> None:
        self.inbox.put_nowait(None)

    async def __aenter__(self) -> "FakeClient":
        return self

    async def __aexit__(self, *_exc: object) -> bool:
        return False

    async def subscribe(self, topic: str, qos: int = 0) -> None:
        pass


@pytest.fixture
def heartbeat(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> Path:
    path = tmp_path / "ingestor-alive"
    monkeypatch.setattr(ingestor, "HEARTBEAT_PATH", path)
    monkeypatch.setattr(ingestor, "HEARTBEAT_INTERVAL", TICK)
    monkeypatch.setattr(ingestor, "STALL_LIMIT", 5 * TICK)
    return path


@pytest.fixture
def client(monkeypatch: pytest.MonkeyPatch) -> FakeClient:
    fake = FakeClient()
    monkeypatch.setattr(ingestor.aiomqtt, "Client", lambda **_kw: fake)
    return fake


def backdate(path: Path) -> None:
    """Make the file look an hour stale, so any later touch is unambiguous."""
    old = time.time() - 3600
    os.utime(path, (old, old))


def fresh(path: Path) -> bool:
    return path.exists() and time.time() - path.stat().st_mtime < 60


async def start(client: FakeClient) -> asyncio.Task[None]:
    task = asyncio.create_task(ingestor.run_once("broker", 1883, None, 0.0))  # type: ignore[arg-type]
    await asyncio.sleep(3 * TICK)
    return task


async def test_idle_session_keeps_the_heartbeat_fresh(heartbeat: Path, client: FakeClient) -> None:
    task = await start(client)
    assert fresh(heartbeat), "the session's first tick must create the file"
    # No device ever publishes. The old code never touched the file again.
    backdate(heartbeat)
    await asyncio.sleep(5 * TICK)
    assert fresh(heartbeat), "an idle, connected ingestor is alive"
    client.drop()
    await task


async def test_a_dropped_session_stops_the_heartbeat(heartbeat: Path, client: FakeClient) -> None:
    task = await start(client)
    client.drop()
    await asyncio.wait_for(task, 1)
    backdate(heartbeat)
    await asyncio.sleep(5 * TICK)
    assert not fresh(heartbeat), "the timer must not outlive the broker connection"


async def test_a_wedged_consumer_stops_the_heartbeat(
    heartbeat: Path, client: FakeClient, monkeypatch: pytest.MonkeyPatch
) -> None:
    release = asyncio.Event()

    async def hung_write(*_args: object) -> None:
        await release.wait()

    monkeypatch.setattr(ingestor, "handle_message", hung_write)
    task = await start(client)
    client.inbox.put_nowait(FakeMessage())
    await asyncio.sleep(10 * TICK)  # past STALL_LIMIT
    backdate(heartbeat)
    await asyncio.sleep(5 * TICK)
    assert not fresh(heartbeat), "a session that cannot consume is not alive"

    # Unwedged, it recovers by itself — no restart needed.
    release.set()
    await asyncio.sleep(5 * TICK)
    assert fresh(heartbeat)
    client.drop()
    await task


async def test_a_failed_write_does_not_stop_the_heartbeat(
    heartbeat: Path, client: FakeClient, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A database outage is not an ingestor fault: restarting it does not fix Postgres."""

    class BrokenSession:
        async def __aenter__(self) -> None:
            raise OSError("postgres unreachable")

        async def __aexit__(self, *_exc: object) -> bool:
            return False

    task = asyncio.create_task(
        ingestor.run_once("broker", 1883, BrokenSession, 0.0)  # type: ignore[arg-type]
    )
    await asyncio.sleep(3 * TICK)
    for _ in range(3):
        client.inbox.put_nowait(FakeMessage())
    await asyncio.sleep(3 * TICK)
    backdate(heartbeat)
    await asyncio.sleep(5 * TICK)
    assert fresh(heartbeat)
    assert not task.done(), "one failed write must not end the session"
    client.drop()
    await task

"""`agent/tools/flaky_link.py` — the link impairment proxy (R2-test-2).

The proxy is how an outage gets between the QEMU board and the dev stack: slirp has none of
its own. The D2/D3 and P1-P3 results are only as good as the proxy's word, so each property
the scenarios lean on is held here against a local echo server:

* `pass` changes no byte, in either direction;
* `blackhole` holds bytes and closes nothing, and delivers everything intact and in order
  afterwards — a proxy that dropped bytes or closed on blackhole would turn a D2 "resumed"
  into a D4 "failed" and make D3's "loops forever" unobservable;
* a connection opened during a blackhole does not reach the server until it ends, and one
  the client abandoned meanwhile never does (the SYN "never arrived");
* `reset` aborts both sides and leaves the previous mode in effect;
* `throttle` really slows the stream down (a loose lower bound only — CI is noisy);
* the scheduler switches on time;
* bad schedules are refused before anything listens.
"""

import asyncio
import importlib.util
import sys
import time
from pathlib import Path
from types import ModuleType
from typing import Any

import pytest

FLAKY_PY = Path(__file__).resolve().parent.parent / "agent" / "tools" / "flaky_link.py"


def _load_module() -> ModuleType:
    spec = importlib.util.spec_from_file_location("flaky_link_tool", FLAKY_PY)
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    # dataclasses resolve string annotations through sys.modules[cls.__module__].
    sys.modules[spec.name] = module
    spec.loader.exec_module(module)
    return module


fl = _load_module()


class Echo:
    """An upstream that echoes every byte and counts the connections it accepted."""

    def __init__(self) -> None:
        self.connections = 0
        self.server: asyncio.Server | None = None
        self.writers: list[asyncio.StreamWriter] = []

    async def start(self) -> int:
        self.server = await asyncio.start_server(self._handle, "127.0.0.1", 0)
        return int(self.server.sockets[0].getsockname()[1])

    async def _handle(self, reader: asyncio.StreamReader, writer: asyncio.StreamWriter) -> None:
        self.connections += 1
        self.writers.append(writer)
        try:
            while data := await reader.read(65536):
                writer.write(data)
                await writer.drain()
        except (ConnectionError, OSError):
            pass
        finally:
            writer.transport.abort()

    async def close(self) -> None:
        assert self.server is not None
        self.server.close()
        for writer in self.writers:
            writer.transport.abort()
        await self.server.wait_closed()


async def _setup(mode: Any = None) -> tuple[Echo, Any]:
    echo = Echo()
    port = await echo.start()
    link = fl.Link([fl.Listen(0, "127.0.0.1", port)], mode=mode or fl.PASS, quiet=True)
    await link.start()
    return echo, link


async def _connect(link: Any) -> tuple[asyncio.StreamReader, asyncio.StreamWriter]:
    return await asyncio.open_connection("127.0.0.1", link.ports[0])


async def _read_exactly(reader: asyncio.StreamReader, n: int, timeout: float) -> bytes:
    return await asyncio.wait_for(reader.readexactly(n), timeout)


# --- parse_schedule ----------------------------------------------------------------------


def test_parse_schedule_accepts_the_runbook_shapes() -> None:
    assert fl.parse_schedule("0=pass") == [(0.0, fl.PASS)]
    assert fl.parse_schedule("0=throttle:8192,20=blackhole,620=reset,621=pass") == [
        (0.0, fl.Mode("throttle", 8192)),
        (20.0, fl.BLACKHOLE),
        (620.0, fl.RESET),
        (621.0, fl.PASS),
    ]
    assert fl.parse_schedule("0=pass,0.3=blackhole", 1.5) == [(0.0, fl.PASS), (0.3, fl.BLACKHOLE)]


@pytest.mark.parametrize(
    ("text", "repeat"),
    [
        ("1=pass", None),  # first entry not at 0
        ("0=pass,5=blackhole,5=pass", None),  # not strictly increasing
        ("0=pass,5=blackhole,3=pass", None),
        ("0=wobble", None),  # unknown mode
        ("0=throttle:0", None),
        ("0=throttle:x", None),
        ("0=throttle", None),
        ("0=reset", None),  # the first entry must be a mode
        ("0pass", None),
        ("0=pass,5=blackhole", 5.0),  # repeat not past the last T
        ("0=pass,5=blackhole", 3.0),
    ],
)
def test_parse_schedule_refuses(text: str, repeat: float | None) -> None:
    with pytest.raises(fl.FlakyLinkError):
        fl.parse_schedule(text, repeat)


def test_parse_listen() -> None:
    assert fl.parse_listen("19000:127.0.0.1:9000") == fl.Listen(19000, "127.0.0.1", 9000)
    for bad in ("19000", "19000:127.0.0.1", "x:127.0.0.1:9000", "1::2"):
        with pytest.raises(fl.FlakyLinkError):
            fl.parse_listen(bad)


def test_main_refuses_a_taken_port_with_exit_2(capsys: pytest.CaptureFixture[str]) -> None:
    import socket

    holder = socket.socket()
    holder.bind(("127.0.0.1", 0))
    holder.listen()
    port = holder.getsockname()[1]
    try:
        assert fl.main(["--listen", f"{port}:127.0.0.1:1"]) == 2
    finally:
        holder.close()
    assert str(port) in capsys.readouterr().err


# --- the proxy ---------------------------------------------------------------------------


async def test_pass_round_trips_bytes_unchanged() -> None:
    echo, link = await _setup()
    try:
        reader, writer = await _connect(link)
        payload = bytes(range(256)) * 400
        writer.write(payload)
        await writer.drain()
        assert await _read_exactly(reader, len(payload), 2.0) == payload
        writer.close()
    finally:
        await link.close()
        await echo.close()


async def test_blackhole_holds_bytes_without_closing_then_delivers_in_order() -> None:
    echo, link = await _setup()
    try:
        reader, writer = await _connect(link)
        writer.write(b"warm")
        assert await _read_exactly(reader, 4, 1.0) == b"warm"

        link.set_mode(fl.BLACKHOLE)
        payload = b"".join(f"{i:05d}".encode() for i in range(2000))
        writer.write(payload)
        await writer.drain()
        with pytest.raises(asyncio.TimeoutError):
            await asyncio.wait_for(reader.read(1), 0.5)
        assert not reader.at_eof()

        link.set_mode(fl.PASS)
        assert await _read_exactly(reader, len(payload), 2.0) == payload
        writer.close()
    finally:
        await link.close()
        await echo.close()


async def test_a_connection_opened_in_a_blackhole_reaches_upstream_only_after_it() -> None:
    echo, link = await _setup(fl.BLACKHOLE)
    try:
        reader, writer = await _connect(link)  # accepted by the proxy
        writer.write(b"early")
        await asyncio.sleep(0.3)
        assert echo.connections == 0

        link.set_mode(fl.PASS)
        assert await _read_exactly(reader, 5, 2.0) == b"early"
        assert echo.connections == 1
        writer.close()
    finally:
        await link.close()
        await echo.close()


async def test_a_connection_abandoned_during_a_blackhole_never_reaches_upstream() -> None:
    # R2-test-2 P1: esp-mqtt gives up on a CONNECT after 10 s and closes. Replaying those
    # stale CONNECTs when the link returns made the broker take the live session over.
    echo, link = await _setup(fl.BLACKHOLE)
    try:
        _reader, writer = await _connect(link)
        writer.write(b"stale CONNECT")
        await writer.drain()
        writer.close()
        await asyncio.sleep(0.2)

        link.set_mode(fl.PASS)
        await asyncio.sleep(0.2)
        assert echo.connections == 0
    finally:
        await link.close()
        await echo.close()


async def test_reset_aborts_both_sides_and_keeps_the_mode() -> None:
    echo, link = await _setup()
    try:
        reader, writer = await _connect(link)
        writer.write(b"x")
        assert await _read_exactly(reader, 1, 1.0) == b"x"
        upstream_writer = echo.writers[0]

        link.set_mode(fl.RESET)
        try:
            data = await asyncio.wait_for(reader.read(1), 1.0)
            assert data == b""
        except ConnectionError:
            pass
        await asyncio.sleep(0.1)
        assert upstream_writer.transport.is_closing()
        assert link.mode == fl.PASS

        reader2, writer2 = await _connect(link)
        writer2.write(b"again")
        assert await _read_exactly(reader2, 5, 1.0) == b"again"
        writer2.close()
    finally:
        await link.close()
        await echo.close()


async def test_reset_during_a_blackhole_ends_the_held_pairs() -> None:
    echo, link = await _setup(fl.BLACKHOLE)
    try:
        reader, _writer = await _connect(link)
        await asyncio.sleep(0.05)
        link.set_mode(fl.RESET)
        try:
            assert await asyncio.wait_for(reader.read(1), 1.0) == b""
        except ConnectionError:
            pass
        await asyncio.sleep(0.05)
        assert echo.connections == 0
        assert link.mode == fl.BLACKHOLE
    finally:
        await link.close()
        await echo.close()


async def test_throttle_slows_the_stream() -> None:
    echo, link = await _setup(fl.Mode("throttle", 20000))
    try:
        reader, writer = await _connect(link)
        payload = b"t" * 20000
        started = time.monotonic()
        writer.write(payload)
        await writer.drain()
        assert await _read_exactly(reader, len(payload), 5.0) == payload
        elapsed = time.monotonic() - started
        assert 0.7 <= elapsed < 3.0, elapsed
        writer.close()
    finally:
        await link.close()
        await echo.close()


async def test_the_scheduler_switches_on_time() -> None:
    echo, link = await _setup()
    scheduler = asyncio.ensure_future(
        fl.run_schedule(link, fl.parse_schedule("0=pass,0.3=blackhole"), None)
    )
    try:
        reader, writer = await _connect(link)
        writer.write(b"before")
        assert await _read_exactly(reader, 6, 0.25) == b"before"

        await asyncio.sleep(0.4)
        assert link.mode == fl.BLACKHOLE
        writer.write(b"after")
        with pytest.raises(asyncio.TimeoutError):
            await asyncio.wait_for(reader.read(1), 0.3)
        writer.close()
    finally:
        scheduler.cancel()
        await asyncio.gather(scheduler, return_exceptions=True)
        await link.close()
        await echo.close()

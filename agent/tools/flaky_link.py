"""Impair the link between an emulated board and the dev stack, on a schedule (R2-test-2).

Runs on the HOST, as a TCP proxy between the QEMU board and the stack's ports. The board's
ff_cfg (`api-base`, `mqtt-uri`) and the api's public URLs (`FF_PUBLIC_BASE_URL`,
`FF_S3_PUBLIC_ENDPOINT_URL`) point at the proxy's ports instead of the real ones, so every
byte of the enroll, MQTT, resolve and download hops passes through here. Standard library
only, like its siblings.

It exists because the emulated board's network is slirp (`-nic user`), and slirp has no
impairment: no loss, no delay, no outage. The R2-test-2 question — what a marginal radio
does to a deploy and to the confirm timer — needs one.

**Fidelity limit.** Slirp is the guest's TCP peer. It ACKs the guest's segments and answers
its keepalive probes whatever happens on the host side. Impairing the host side therefore
gives the guest exactly one picture of an outage: *the far end is alive but silent*. That is
the worst case for stall detection and right for timing. It does NOT model lwIP
retransmission and loss, Wi-Fi disassociation, DHCP renewal or the TX-power ladder. Results
are "proven in QEMU"; the bench replay is owed (`docs/runbooks/rollback-test.md`).

Modes, applied to every connection, live and new, on every listener of the process:

* `pass` — forward both directions as fast as possible;
* `throttle:N` — forward, each direction capped at N bytes/s;
* `blackhole` — forward nothing and close nothing. The pumps stop reading, so backpressure
  builds in the kernel buffers as on a dead radio. A new connection is accepted and held,
  and its upstream connect is deferred until the blackhole ends; if the client closes it
  first, it never reaches upstream at all (its SYN "never got through"). Held bytes are
  delivered intact and in order afterwards: TCP is reliable, data after an outage is late,
  not lost;
* `reset` — an action, not a mode: abort every live connection pair at that instant. The
  previous mode stays in effect.

    python3 -u agent/tools/flaky_link.py --listen 19000:127.0.0.1:9000 \\
        --schedule "0=throttle:8192,20=blackhole,620=reset,621=pass" [--repeat 30]
"""

from __future__ import annotations

import argparse
import asyncio
import contextlib
import signal
import sys
import time
from dataclasses import dataclass

MODE_NAMES = ("pass", "throttle", "blackhole", "reset")
PASS_CHUNK = 64 * 1024


class FlakyLinkError(RuntimeError):
    """The arguments make no sense, or a listen port cannot be bound. Fatal."""


@dataclass(frozen=True)
class Mode:
    kind: str  # one of MODE_NAMES
    rate: int = 0  # bytes/s, throttle only

    def __str__(self) -> str:
        return f"throttle:{self.rate}" if self.kind == "throttle" else self.kind

    @property
    def chunk(self) -> int:
        return max(1, self.rate // 10) if self.kind == "throttle" else PASS_CHUNK


PASS = Mode("pass")
BLACKHOLE = Mode("blackhole")
RESET = Mode("reset")


@dataclass(frozen=True)
class Listen:
    port: int
    host: str
    remote_port: int

    def __str__(self) -> str:
        return f"{self.port}:{self.host}:{self.remote_port}"


def parse_mode(text: str) -> Mode:
    name, _, arg = text.strip().partition(":")
    if name == "throttle":
        if not arg.isdigit() or int(arg) <= 0:
            raise FlakyLinkError(f"throttle needs a positive whole bytes/s, got {text!r}")
        return Mode("throttle", int(arg))
    if name in ("pass", "blackhole", "reset") and not arg:
        return Mode(name)
    raise FlakyLinkError(f"unknown mode {text!r} (pass, throttle:N, blackhole, reset)")


def parse_schedule(text: str, repeat: float | None = None) -> list[tuple[float, Mode]]:
    """`"0=pass,3=blackhole"` -> `[(0.0, PASS), (3.0, BLACKHOLE)]`, or FlakyLinkError."""
    entries: list[tuple[float, Mode]] = []
    for item in text.split(","):
        when, sep, mode_text = item.partition("=")
        if not sep:
            raise FlakyLinkError(f"schedule entry {item!r} is not T=MODE")
        try:
            at = float(when)
        except ValueError:
            raise FlakyLinkError(f"schedule time {when!r} is not a number") from None
        if at < 0:
            raise FlakyLinkError(f"schedule time {when!r} is negative")
        if entries and at <= entries[-1][0]:
            raise FlakyLinkError(f"schedule times must strictly increase, {at:g} does not")
        entries.append((at, parse_mode(mode_text)))
    if entries[0][0] != 0:
        raise FlakyLinkError("the first schedule entry must be at T=0")
    if entries[0][1] == RESET:
        raise FlakyLinkError("the first schedule entry must be a mode, not reset")
    if repeat is not None and repeat <= entries[-1][0]:
        raise FlakyLinkError(
            f"--repeat {repeat:g} must be greater than the last schedule time {entries[-1][0]:g}"
        )
    return entries


def parse_listen(text: str) -> Listen:
    parts = text.split(":")
    if len(parts) != 3 or not parts[0].isdigit() or not parts[2].isdigit() or not parts[1]:
        raise FlakyLinkError(f"--listen {text!r} is not LPORT:HOST:RPORT")
    return Listen(int(parts[0]), parts[1], int(parts[2]))


class _Pair:
    """One accepted client connection and, once connected, its upstream."""

    def __init__(self, number: int, listen: Listen, writer: asyncio.StreamWriter) -> None:
        self.number = number
        self.listen = listen
        self.client = writer
        self.upstream: asyncio.StreamWriter | None = None
        self.up_bytes = 0
        self.down_bytes = 0
        self.dead = asyncio.Event()

    def abort(self) -> None:
        self.dead.set()
        for writer in (self.client, self.upstream):
            if writer is not None:
                writer.transport.abort()


class Link:
    """The listeners, the current mode, and every live connection pair."""

    def __init__(
        self,
        listens: list[Listen],
        bind: str = "127.0.0.1",
        mode: Mode = PASS,
        quiet: bool = False,
    ) -> None:
        if mode == RESET:
            raise FlakyLinkError("the initial mode cannot be reset")
        self.listens = listens
        self.bind = bind
        self.quiet = quiet
        self.mode = mode
        self.ports: list[int] = []
        self._flowing = asyncio.Event()
        if mode != BLACKHOLE:
            self._flowing.set()
        self._servers: list[asyncio.Server] = []
        self._pairs: set[_Pair] = set()
        self._tasks: set[asyncio.Task[None]] = set()
        self._count = 0
        self._t0 = time.monotonic()

    def log(self, message: str) -> None:
        if not self.quiet:
            stamp = time.strftime("%H:%M:%S")
            print(f"[{time.monotonic() - self._t0:8.3f}] {stamp} {message}", flush=True)

    async def start(self) -> None:
        for listen in self.listens:
            try:
                server = await asyncio.start_server(
                    lambda r, w, listen=listen: self._accept(listen, r, w), self.bind, listen.port
                )
            except OSError as exc:
                await self.close()
                raise FlakyLinkError(f"cannot listen on {self.bind}:{listen.port}: {exc}") from exc
            self._servers.append(server)
            self.ports.append(server.sockets[0].getsockname()[1])
        self.log(f"listening {', '.join(str(listen) for listen in self.listens)}; mode {self.mode}")

    def set_mode(self, mode: Mode) -> None:
        if mode == RESET:
            self.reset()
            return
        self.log(f"mode {self.mode} -> {mode}")
        self.mode = mode
        if mode == BLACKHOLE:
            self._flowing.clear()
        else:
            self._flowing.set()

    def reset(self) -> None:
        pairs = list(self._pairs)
        for pair in pairs:
            pair.abort()
        self.log(f"reset: aborted {len(pairs)} connections (mode stays {self.mode})")

    async def close(self) -> None:
        for server in self._servers:
            server.close()
        for pair in list(self._pairs):
            pair.abort()
        for task in list(self._tasks):
            task.cancel()
        for server in self._servers:
            with contextlib.suppress(Exception):
                await server.wait_closed()
        if self._tasks:
            await asyncio.gather(*self._tasks, return_exceptions=True)
        self._servers.clear()

    async def _gate(self, pair: _Pair) -> bool:
        """Wait until the link is not blackholed. False if the pair died meanwhile."""
        if not self._flowing.is_set():
            waits = [
                asyncio.ensure_future(self._flowing.wait()),
                asyncio.ensure_future(pair.dead.wait()),
            ]
            try:
                await asyncio.wait(waits, return_when=asyncio.FIRST_COMPLETED)
            finally:
                for waiter in waits:
                    waiter.cancel()
        return not pair.dead.is_set()

    async def _defer(self, pair: _Pair, client_r: asyncio.StreamReader) -> bytes | None:
        """Hold a new connection until the link is not blackholed.

        Returns the bytes the client sent meanwhile (to be delivered first), or None when
        the connection must never reach upstream: the pair was reset, or the client gave
        up and closed it during the blackhole. On a real link that connection's SYN never
        got through, so the server never saw it; replaying it later (an MQTT CONNECT that
        takes over the live session, say) would be an artifact of this proxy.
        """
        if self._flowing.is_set():
            return b""
        pending = bytearray()

        async def drain_client() -> None:
            while data := await client_r.read(PASS_CHUNK):
                pending.extend(data)

        reading = asyncio.ensure_future(drain_client())
        waits = [
            asyncio.ensure_future(self._flowing.wait()),
            asyncio.ensure_future(pair.dead.wait()),
        ]
        try:
            await asyncio.wait([reading, *waits], return_when=asyncio.FIRST_COMPLETED)
        finally:
            for waiter in waits:
                waiter.cancel()
        if reading.done():
            with contextlib.suppress(Exception):
                reading.result()
            if not pair.dead.is_set():
                self.log(
                    f"conn {pair.number} abandoned by the client during the blackhole "
                    f"({len(pending)} B never reached upstream)"
                )
            return None
        reading.cancel()
        with contextlib.suppress(asyncio.CancelledError, Exception):
            await reading
        return None if pair.dead.is_set() else bytes(pending)

    def _accept(self, listen: Listen, reader: asyncio.StreamReader, writer: asyncio.StreamWriter):
        task = asyncio.ensure_future(self._serve(listen, reader, writer))
        self._tasks.add(task)
        task.add_done_callback(self._tasks.discard)

    async def _serve(
        self, listen: Listen, client_r: asyncio.StreamReader, client_w: asyncio.StreamWriter
    ) -> None:
        self._count += 1
        pair = _Pair(self._count, listen, client_w)
        self._pairs.add(pair)
        self.log(f"conn {pair.number} open {listen.port} -> {listen.host}:{listen.remote_port}")
        try:
            # A SYN sent into a blackhole never reaches the server: defer the connect.
            pending = await self._defer(pair, client_r)
            if pending is None or client_w.transport.is_closing():
                return
            try:
                up_r, up_w = await asyncio.open_connection(listen.host, listen.remote_port)
            except OSError as exc:
                self.log(f"conn {pair.number} upstream connect failed: {exc}")
                client_w.transport.abort()
                return
            pair.upstream = up_w
            self.log(f"conn {pair.number} upstream connected")
            if pending:
                up_w.write(pending)
                pair.up_bytes += len(pending)
            await asyncio.gather(
                self._pump(pair, client_r, up_w, "up"),
                self._pump(pair, up_r, client_w, "down"),
            )
        finally:
            pair.abort()
            self._pairs.discard(pair)
            self.log(f"conn {pair.number} closed (up {pair.up_bytes} B, down {pair.down_bytes} B)")

    async def _pump(
        self,
        pair: _Pair,
        reader: asyncio.StreamReader,
        writer: asyncio.StreamWriter,
        direction: str,
    ) -> None:
        try:
            while True:
                if not await self._gate(pair):
                    return
                mode = self.mode
                data = await reader.read(mode.chunk)
                if not data:
                    if writer.can_write_eof() and not writer.transport.is_closing():
                        writer.write_eof()
                    return
                # Bytes read just before a blackhole began are held, not delivered.
                if not await self._gate(pair):
                    return
                writer.write(data)
                await writer.drain()
                if direction == "up":
                    pair.up_bytes += len(data)
                else:
                    pair.down_bytes += len(data)
                if self.mode.kind == "throttle":
                    await asyncio.sleep(len(data) / self.mode.rate)
        except (ConnectionError, OSError):
            pair.abort()


async def run_schedule(link: Link, entries: list[tuple[float, Mode]], repeat: float | None):
    loop = asyncio.get_running_loop()
    start = loop.time()
    cycle = 0
    while True:
        for at, mode in entries:
            delay = start + cycle * (repeat or 0) + at - loop.time()
            if delay > 0:
                await asyncio.sleep(delay)
            if mode != link.mode or mode == RESET:
                link.set_mode(mode)
        if repeat is None:
            return
        cycle += 1


async def serve(listens: list[Listen], entries: list[tuple[float, Mode]], args) -> None:
    link = Link(listens, bind=args.bind, mode=entries[0][1])
    await link.start()
    stop = asyncio.Event()
    loop = asyncio.get_running_loop()
    for sig in (signal.SIGINT, signal.SIGTERM):
        loop.add_signal_handler(sig, stop.set)
    scheduler = asyncio.ensure_future(run_schedule(link, entries, args.repeat))
    try:
        await stop.wait()
    finally:
        scheduler.cancel()
        with contextlib.suppress(asyncio.CancelledError):
            await scheduler
        link.log("stopping")
        await link.close()


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument(
        "--listen",
        action="append",
        required=True,
        metavar="LPORT:HOST:RPORT",
        help="accept on BIND:LPORT, forward to HOST:RPORT (repeatable)",
    )
    parser.add_argument(
        "--schedule", default="0=pass", help='"T=MODE,..." seconds since start (default 0=pass)'
    )
    parser.add_argument("--repeat", type=float, default=None, help="loop the schedule every N s")
    parser.add_argument("--bind", default="127.0.0.1", help="address to listen on")
    args = parser.parse_args(argv)

    try:
        listens = [parse_listen(text) for text in args.listen]
        entries = parse_schedule(args.schedule, args.repeat)
        asyncio.run(serve(listens, entries, args))
        return 0
    except FlakyLinkError as exc:
        print(f"flaky_link: {exc}", file=sys.stderr)
        return 2


if __name__ == "__main__":
    raise SystemExit(main())

"""`python -m fleetforge.simulator` — a board that speaks the R0 protocol, no hardware.

```
just sim --token ffe_… --name blinker --heartbeat-interval 5
just sim --name blinker --duration 10                  # reuses .sim/, enrolls nothing
just sim --token ffe_… --power-class sleepy --wake-interval 20 --awake-s 3
just sim --name blinker --crash-after 15                # ungraceful: the LWT fires
just sim-fleet 3 --heartbeat-interval 5                 # issues its own tokens
docker compose exec -T api python -m fleetforge.simulator run --host mosquitto --port 1883 …
```

The six steps of `spec/device-protocol.md` → *Enrolment happens over HTTPS, not MQTT*,
in Python: baked config as CLI flags → `POST /v1/enroll` → write the credential →
connect → `up/announce` + `up/presence` (both retained) → `up/hb` forever.

Same shape as `broker/__main__.py` and `storage/__main__.py`: argparse with a
subcommand, one `_step()` line per action so the run reads as a transcript, and every
expected failure family funnelled in `main()` into `SIMULATOR FAILED: <Type>: <msg>`
rather than a traceback.

**Nothing here prints a secret.** Not the broker password (which is written 0600 into
`.sim/`, gitignored — `state.py`), not the admin password, not an `ffa_` session token,
not an `ffe_` plaintext. Enrollment token **ids** are printed, because
`routers/enroll.py` already logs them and they are how an operator finds the row.

Two ordering rules that are the reason this file is longer than it looks:

* **Everything that can be validated is validated before a token is presented.** A
  single-use token spent on a 422 is a re-issue in dev and a re-flash in the field, so
  `DeviceIdentity.validate()` and `LinkProfile` construction both run first — and for
  `fleet`, *every* board's identity is validated before the first token is issued.
* **A state file present means no enrollment at all.** Not "re-enroll if it looks
  stale": that would burn a token silently. `--forget` is the deliberate opposite.
"""

import argparse
import asyncio
import contextlib
import datetime as dt
import os
import signal
import sys
from collections.abc import Awaitable, Callable
from pathlib import Path

import aiomqtt

from fleetforge.simulator.client import enroll, issue_enrollment_token, login
from fleetforge.simulator.device import (
    DEFAULT_AWAKE_S,
    DEFAULT_HEARTBEAT_INTERVAL_S,
    LINK_FAST,
    LINK_PROFILES,
    POWER_CLASSES,
    DeviceIdentity,
    LinkProfile,
    Step,
    derive_device_id,
    mqtt_client_factory,
    run_always_on,
    run_sleepy,
)
from fleetforge.simulator.errors import SimulatorError
from fleetforge.simulator.state import Credential, describe, forget, load, save, state_path

# The same variables `.env` and the justfile already define — nothing new is added to
# `.env.example`. Note that `just` does not export `.env` into a recipe, so these are
# read from a real environment; the defaults below are the dev stack's values.
DEFAULT_HTTP_PORT = "8080"
DEFAULT_MQTT_HOST = "localhost"
DEFAULT_MQTT_PORT = "8883"
DEFAULT_STATE_DIR = ".sim"


def _step(message: str) -> None:
    """One line per action, to stdout, so the whole run reads as a transcript."""
    print(message, flush=True)


def _prefixed(name: str) -> Step:
    """`fleet` runs N boards in one process; every line says which board it came from."""

    def step(message: str) -> None:
        print(f"{name:<10} {message}", flush=True)

    return step


def _default_api_base() -> str:
    base = os.environ.get("FF_API_BASE")
    if base:
        return base
    return f"http://localhost:{os.environ.get('FF_HTTP_PORT', DEFAULT_HTTP_PORT)}"


def _now_iso() -> str:
    """UTC, second resolution, `Z`-suffixed. Advisory only — the server timestamps."""
    return dt.datetime.now(dt.UTC).replace(microsecond=0).isoformat().replace("+00:00", "Z")


def _identity_for(args: argparse.Namespace, device_id: str) -> DeviceIdentity:
    """Build the announce identity from the flags. No I/O, no validation yet."""
    return DeviceIdentity(
        device_id=device_id,
        platform_type=args.platform_type,
        fw_version=args.fw_version,
        agent_version=args.agent_version,
        link_type=args.link_type,
        power_class=args.power_class,
        expected_wake_interval_s=args.wake_interval,
        partition_layout=args.partition_layout,
        ota_slot_size=args.ota_slot_size,
        capabilities=tuple(args.capabilities or ()),
    )


async def _credential_for(
    args: argparse.Namespace,
    identity: DeviceIdentity,
    token: str | None,
    link: LinkProfile,
    step: Step,
) -> Credential:
    """Load this board's credential, or enroll once to get one.

    The whole CRITICAL surface of this task is in these thirty lines: a token is spent
    only when there is genuinely no credential on disk, the response is written to a
    0600 file *before* the broker is contacted (the real agent's order, and the reason
    `enroll_retry_window_s` exists), and the password never reaches the transcript.
    """
    state_dir = Path(args.state_dir)
    device_id = identity.device_id

    if args.forget and forget(state_dir, device_id):
        step(f"forget   removed {state_path(state_dir, device_id)}; this run will re-enroll")

    credential = load(state_dir, device_id)
    if credential is not None:
        step(f"state    reusing {state_path(state_dir, device_id)} (no enrollment)")
        return credential

    if not token:
        raise SimulatorError(
            f"{device_id} has no credential in {state_dir}/ and no --token was given. A board "
            "with no credential must enroll, and enrolling needs a single-use ffe_ token: "
            "POST /v1/enrollment-tokens, or use `just sim-fleet` which issues its own."
        )

    delay_s = link.enroll_delay()
    if delay_s:
        step(f"link     +{delay_s:.2f}s of latency on the enroll POST")
    result, status = await enroll(args.api_base, identity.enroll_body(token), delay_s=delay_s)
    step(f"enroll   {status} {args.api_base}/v1/enroll -> device_id {result.device_id}")

    credential = Credential(
        device_id=result.device_id,
        mqtt_username=result.mqtt_username,
        mqtt_password=result.mqtt_password,
        api_base=args.api_base,
        enrolled_at=_now_iso(),
    )
    path = save(state_dir, credential)
    step(f"state    {describe(path)}")
    return credential


async def _run_board(
    args: argparse.Namespace,
    identity: DeviceIdentity,
    *,
    token: str | None,
    stop: asyncio.Event,
    step: Step,
) -> None:
    """Enroll if needed, then be a board until `stop` is set."""
    link = LinkProfile(args.link, args.seed)
    credential = await _credential_for(args, identity, token, link, step)

    factory = mqtt_client_factory(
        args.host, args.port, credential, identity.device_id, tls=args.tls
    )
    endpoint = f"{args.host}:{args.port}"

    if identity.power_class == "sleepy":
        assert identity.expected_wake_interval_s is not None  # noqa: S101 - validate() proved it
        await run_sleepy(
            identity,
            credential,
            client_factory=factory,
            link=link,
            heartbeat_interval_s=args.heartbeat_interval,
            wake_interval_s=float(identity.expected_wake_interval_s),
            awake_s=args.awake_s,
            stop=stop,
            endpoint=endpoint,
            step=step,
        )
        return

    await run_always_on(
        identity,
        credential,
        client_factory=factory,
        link=link,
        heartbeat_interval_s=args.heartbeat_interval,
        stop=stop,
        endpoint=endpoint,
        step=step,
    )


async def _stop_after(seconds: float, stop: asyncio.Event, step: Step) -> None:
    """`--duration`: shut down *cleanly*, so the goodbye is published."""
    await asyncio.sleep(seconds)
    step(f"duration {seconds:.0f}s elapsed; shutting down cleanly")
    stop.set()


async def _crash_after(seconds: float, step: Step) -> None:
    """`--crash-after`: the only honest way to make the broker publish the will.

    `os._exit(1)` skips every atexit hook and every `finally:` — the kernel closes the
    socket with a TCP FIN and **no MQTT DISCONNECT packet**, which is precisely the
    condition Mosquitto publishes an LWT on. Do not try to do this through aiomqtt: it
    has no public API for dropping a connection and `client._client` is private.
    """
    await asyncio.sleep(seconds)
    step(f"crash    os._exit(1) after {seconds:.0f}s — no DISCONNECT, so the broker fires the LWT")
    sys.stdout.flush()
    sys.stderr.flush()
    os._exit(1)


def _install_signal_handlers(stop: asyncio.Event) -> None:
    """Ctrl-C and SIGTERM ask for a clean stop, so the goodbye still gets published."""
    loop = asyncio.get_running_loop()
    for sig in (signal.SIGINT, signal.SIGTERM):
        with contextlib.suppress(NotImplementedError):
            loop.add_signal_handler(sig, stop.set)


async def _with_timers(
    args: argparse.Namespace, stop: asyncio.Event, body: Callable[[], Awaitable[None]]
) -> None:
    """Run `body` alongside `--duration` / `--crash-after`, whichever were asked for."""
    timers: list[asyncio.Task[None]] = []
    if args.duration is not None:
        timers.append(asyncio.create_task(_stop_after(args.duration, stop, _step)))
    if args.crash_after is not None:
        timers.append(asyncio.create_task(_crash_after(args.crash_after, _step)))
    try:
        await body()
    finally:
        for timer in timers:
            timer.cancel()
        await asyncio.gather(*timers, return_exceptions=True)


async def _run(args: argparse.Namespace) -> None:
    """The `run` subcommand: one board."""
    device_id = args.device_id or derive_device_id(args.name)
    identity = _identity_for(args, device_id)
    # BEFORE anything that can spend a token or open a socket.
    identity.validate()

    _step(f"board    {args.name}")
    _step(
        f"device   {device_id}"
        + ("" if args.device_id else f" (derived from the name {args.name!r})")
    )
    _step(f"identity {identity.platform_type} fw {identity.fw_version} · {identity.power_class}")

    stop = asyncio.Event()
    _install_signal_handlers(stop)

    async def body() -> None:
        await _run_board(args, identity, token=args.token, stop=stop, step=_step)

    await _with_timers(args, stop, body)


async def _run_fleet(args: argparse.Namespace) -> None:
    """The `fleet` subcommand: N boards in one process, issuing their own tokens.

    Tokens are single-use, so typing three of them by hand is the friction this task
    exists to remove. Issuance needs the admin **password** (not the hash), which is
    never printed; each issued token's **id** is.
    """
    names = [f"{args.prefix}-{index:02d}" for index in range(1, args.count + 1)]
    identities = {name: _identity_for(args, derive_device_id(name)) for name in names}
    for identity in identities.values():
        identity.validate()
    LinkProfile(args.link, args.seed)  # fail on a bad --link before logging in

    state_dir = Path(args.state_dir)
    if args.forget:
        for name, identity in identities.items():
            if forget(state_dir, identity.device_id):
                _step(f"forget   removed {state_path(state_dir, identity.device_id)} ({name})")

    needs_token = [
        name for name, ident in identities.items() if load(state_dir, ident.device_id) is None
    ]
    tokens: dict[str, str] = {}
    if needs_token:
        password = args.admin_password or os.environ.get("FF_ADMIN_PASSWORD")
        if not password:
            raise SimulatorError(
                "`fleet` issues its own enrollment tokens and therefore needs the admin "
                "PASSWORD (not ADMIN_PASSWORD_HASH): pass --admin-password or set "
                "$FF_ADMIN_PASSWORD."
            )
        admin_token = await login(args.api_base, password)
        _step("login    admin session acquired (the ffa_ token is never printed)")
        for name in needs_token:
            token_id, plaintext = await issue_enrollment_token(args.api_base, admin_token)
            tokens[name] = plaintext
            _step(f"issued   enrollment token {token_id} for {name}")
    for name, identity in identities.items():
        _step(f"board    {name} -> {identity.device_id}")

    stop = asyncio.Event()
    _install_signal_handlers(stop)

    async def body() -> None:
        async with asyncio.TaskGroup() as group:
            for name, identity in identities.items():
                group.create_task(
                    _run_board(
                        args,
                        identity,
                        token=tokens.get(name),
                        stop=stop,
                        step=_prefixed(name),
                    )
                )

    await _with_timers(args, stop, body)


def _shared(parser: argparse.ArgumentParser) -> None:
    """Flags both subcommands take. Environment defaults, CLI overrides."""
    parser.add_argument("--api-base", default=_default_api_base(), help="origin serving /v1")
    parser.add_argument("--host", default=os.environ.get("MQTT_HOST", DEFAULT_MQTT_HOST))
    parser.add_argument(
        "--port", type=int, default=int(os.environ.get("MQTT_PORT", DEFAULT_MQTT_PORT))
    )
    parser.add_argument(
        "--tls",
        action="store_true",
        default=os.environ.get("MQTT_TLS", "").lower() in ("1", "true", "yes"),
        help=(
            "MQTT over TLS. Required against production (bingo.tvaroska.sk:8883); "
            "the dev broker is plaintext behind Traefik, so this is off by default. "
            "Without it a TLS listener just hangs the connect until it times out."
        ),
    )
    parser.add_argument("--platform-type", default="esp32c6")
    parser.add_argument("--fw-version", default="1.4.2")
    parser.add_argument(
        "--agent-version",
        default="0.1.0-sim",
        help="says -sim on purpose: a fake board must be identifiable in the fleet list",
    )
    parser.add_argument("--link-type", default="wifi")
    parser.add_argument("--partition-layout", default="ab-4m-v1")
    parser.add_argument("--ota-slot-size", type=int, default=1966080)
    parser.add_argument("--power-class", default="always_on", choices=POWER_CLASSES)
    parser.add_argument(
        "--wake-interval",
        type=int,
        default=None,
        help="seconds; REQUIRED for --power-class sleepy (checked before the token is spent)",
    )
    parser.add_argument(
        "--heartbeat-interval",
        type=float,
        default=DEFAULT_HEARTBEAT_INTERVAL_S,
        help="seconds (spec/prd.md -> Timing). Use 5 for a demo; the default is the spec number",
    )
    parser.add_argument("--awake-s", type=float, default=DEFAULT_AWAKE_S)
    parser.add_argument("--capabilities", action="append", default=[])
    parser.add_argument("--link", default=LINK_FAST, choices=LINK_PROFILES)
    parser.add_argument("--seed", type=int, default=0, help="makes --link slow reproducible")
    parser.add_argument(
        "--duration", type=float, default=None, help="exit cleanly (with the goodbye) after N s"
    )
    parser.add_argument(
        "--crash-after",
        type=float,
        default=None,
        help="os._exit(1) after N s: no DISCONNECT, so the broker publishes the LWT",
    )
    parser.add_argument(
        "--state-dir",
        default=os.environ.get("FF_SIM_STATE_DIR", DEFAULT_STATE_DIR),
        help="gitignored, 0600 files; holds the broker credential (the NVS analogue)",
    )
    parser.add_argument(
        "--forget",
        action="store_true",
        help="delete this board's state file first, so the next run enrolls again",
    )


def build_parser() -> argparse.ArgumentParser:
    """The CLI. Exposed so the tests can parse argv without running anything."""
    parser = argparse.ArgumentParser(
        prog="python -m fleetforge.simulator",
        description="A simulated ESP32 board that speaks the R0 device protocol.",
    )
    subparsers = parser.add_subparsers(dest="command", required=True)

    run_parser = subparsers.add_parser("run", help="one simulated board")
    _shared(run_parser)
    run_parser.add_argument("--token", default=None, help="an ffe_ enrollment token (single-use)")
    run_parser.add_argument("--name", default="sim", help="the device_id is derived from this")
    run_parser.add_argument(
        "--device-id", default=None, help="override the derived id (still DEVICE_ID_RE-validated)"
    )

    fleet_parser = subparsers.add_parser("fleet", help="N boards, issuing their own tokens")
    _shared(fleet_parser)
    fleet_parser.add_argument("--count", type=int, default=3)
    fleet_parser.add_argument(
        "--prefix", default="sim", help="names are <prefix>-01, <prefix>-02, …"
    )
    fleet_parser.add_argument(
        "--admin-password", default=None, help="or $FF_ADMIN_PASSWORD; never printed"
    )
    return parser


# SimulatorError is a RuntimeError; urllib's URLError/HTTPError are OSErrors; aiomqtt's
# MqttError is neither. Anything outside this set is a programming error and keeps its
# traceback — a bare `except Exception` here would hide exactly the bugs worth seeing.
EXPECTED_FAILURES = (SimulatorError, aiomqtt.MqttError, OSError, ValueError)


def flatten(exc: BaseException) -> list[BaseException]:
    """One flat list of leaf exceptions, unwrapping nested `ExceptionGroup`s.

    `fleet` runs its boards in an `asyncio.TaskGroup`, so five boards failing to reach
    the API arrive as one `ExceptionGroup` of five `SimulatorError`s. Printing the
    group's `str()` would print `unhandled errors in a TaskGroup (5 sub-exceptions)`,
    which tells the operator nothing.
    """
    if isinstance(exc, BaseExceptionGroup):
        leaves: list[BaseException] = []
        for sub in exc.exceptions:
            leaves.extend(flatten(sub))
        return leaves
    return [exc]


def main(argv: list[str] | None = None) -> int:
    """Run a board. Returns a process exit code; `SIMULATOR OK` means a clean shutdown."""
    args = build_parser().parse_args(argv)
    runner = _run_fleet if args.command == "fleet" else _run
    try:
        asyncio.run(runner(args))
    except (*EXPECTED_FAILURES, BaseExceptionGroup) as exc:
        failures = flatten(exc)
        if not all(isinstance(failure, EXPECTED_FAILURES) for failure in failures):
            raise  # a group carrying something unexpected: keep the traceback
        for failure in failures:
            # Printed with its type because "which failure was it" is the whole
            # question, and a traceback answers it badly.
            print(f"SIMULATOR FAILED: {type(failure).__name__}: {failure}", file=sys.stderr)
        return 1
    print("SIMULATOR OK")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

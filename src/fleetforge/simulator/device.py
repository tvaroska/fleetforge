"""What a simulated board *is* on the wire: identity, topics, and one MQTT session.

Everything here is a reading of `spec/device-protocol.md` (near-frozen, **CRITICAL**),
never an extension of it. Pure message building is split from the I/O the same way
`broker/dynsec.py` splits command building from the transport, so
`tests/test_simulator.py` can drive a whole session with **no broker running** by
passing its own `client_factory`.

Six properties that are easy to get wrong, all encoded below:

1. **The QoS/retain matrix is the contract, and a wrong retain flag is a silent wrong
   answer rather than an error.** `announce` and `presence` are retained; `hb` is
   **not**. A retained heartbeat would be replayed to the ingestor on every reconnect,
   and `ingestor/handlers.py` treats a retained message as a replay — so `last_seen`
   would quietly stop advancing, in exactly the way that is hardest to notice.
2. **The LWT is retained too.** Without the flag a server that restarts after a board
   died never learns it is offline, which is the precise failure retained state exists
   to prevent. (`spec/device-protocol.md` says `up/presence` is retained and that the
   LWT publishes `{"online":false}`; that the *will* carries the flag is a proposed
   addition — see `DECISIONS.md`, R0-test-1.)
3. **`clean_session=False` with `identifier=device_id`.** Command durability comes
   from the persistent session, never from a retained `dn/cmd`, and MQTT 3.1.1
   requires a non-empty client id for one. R0-be-4 PROPOSED `client_id = device_id`
   for the agent; this implements the proposal.
4. **A clean disconnect does NOT fire the LWT** — Mosquitto publishes the will only
   when the socket drops without a DISCONNECT packet. So a graceful shutdown must
   publish its own retained `{"online":false}` *goodbye*, or Ctrl-C leaves a board
   showing online in the dashboard forever. `__main__.py --crash-after` is the only
   honest way to exercise the real will (`os._exit`, a TCP FIN with no DISCONNECT);
   aiomqtt has no public API for dropping a connection.
5. **`aiomqtt.Client` is not re-enterable** (`MqttReentrantError`, 2.5.1). The sleepy
   loop therefore builds a **new** client per wake, which is why the client arrives as
   a factory rather than as an object.
6. **A denied publish is invisible below MQTT v5** (`DECISIONS.md`, R0-sec-1): paho
   reports success and the broker silently drops. "It published and nothing happened"
   is a credential or ACL symptom, never a bug in the publish call — which is why the
   transcript prints every publish and the operator checks `GET /v1/devices`.

*Known limitation of the sleepy mode:* a real deep-sleeping board drops TCP without a
DISCONNECT and therefore fires its LWT on every sleep, while this one disconnects
cleanly. The server-visible state is identical — the ingestor never advances
`last_seen` on `presence{online:false}` and `presence.is_online` ignores
`presence_reported` for sleepy boards — and forcing an ungraceful drop would mean
private-API surgery on `aiomqtt`.
"""

import asyncio
import contextlib
import hashlib
import json
import random
import time
from collections.abc import Callable
from dataclasses import dataclass, field

import aiomqtt

from fleetforge.identity import DEVICE_ID_RE
from fleetforge.simulator.errors import SimulatorError
from fleetforge.simulator.state import Credential

# `spec/device-protocol.md` → *Topic namespace*. Spelled once, here.
TOPIC_ROOT = "ff/v1/d"

ANNOUNCE = "announce"
PRESENCE = "presence"
HEARTBEAT = "hb"

# Compact and byte-exact, because the will payload is compared as bytes in the tests
# and read by a human in `mosquitto_sub` output.
PRESENCE_ONLINE = b'{"online":true}'
PRESENCE_OFFLINE = b'{"online":false}'

QOS = 1

# 30 s: comfortably under any NAT idle timeout, and short enough that an always_on
# board's LWT fires within ~45 s of the socket dying rather than minutes later.
KEEPALIVE_S = 30

# `spec/prd.md` → *Requirements & targets → Timing*.
DEFAULT_HEARTBEAT_INTERVAL_S = 60.0
# How long a sleepy board stays connected per wake — long enough to drain queued
# `dn/` commands from the persistent session, short enough to be a real duty cycle.
DEFAULT_AWAKE_S = 3.0

# `--link slow`: a delay drawn from this range before every publish, plus the extra
# on the enroll POST. Deliberately not a packet-drop simulation — QoS 1 over a
# persistent session does not lose messages, and pretending otherwise would teach the
# operator something untrue about the protocol.
SLOW_MIN_DELAY_S = 0.5
SLOW_MAX_DELAY_S = 3.0
SLOW_ENROLL_EXTRA_S = 2.0

LINK_FAST = "fast"
LINK_SLOW = "slow"
LINK_PROFILES = (LINK_FAST, LINK_SLOW)

# `db/models.py::PowerClass`, retyped rather than imported: `fleetforge.db` drags
# SQLAlchemy into a process that is pretending to be an ESP32 (see `__init__.py`).
# There is a tripwire test asserting the two agree.
POWER_CLASSES = ("always_on", "sleepy")

ClientFactory = Callable[[], aiomqtt.Client]
Step = Callable[[str], None]


def _silent(_message: str) -> None:
    """The default transcript sink: a library call prints nothing."""


def derive_device_id(name: str) -> str:
    """A stable, locally-administered pseudo-MAC for a simulated board named `name`.

    Stable across runs, so `just sim --name blinker` is always the *same* board in the
    dashboard, and provably not a real MAC:

    * bit 1 of the first octet set = **locally administered**;
    * bit 0 cleared = **unicast** (IEEE 802).

    An Espressif eFuse MAC is neither, so a simulated board can never collide with
    real hardware. Clearing bit 0 also makes a leading `ff` octet unreachable (0xff is
    odd), so `broker/__main__.py`'s reserved `ffff…` selftest ids stay reserved. Both
    properties are asserted in `tests/test_simulator.py`, not hoped for.

    `bytes.hex()` is already lowercase, so the result satisfies `DEVICE_ID_RE` without
    a `.lower()` anywhere — `identity.py`'s rule is *never normalise, always reject*.
    """
    digest = hashlib.sha256(name.encode("utf-8")).digest()
    first = (digest[0] & 0xFE) | 0x02
    return bytes([first, *digest[1:6]]).hex()


def up_topic(device_id: str, channel: str) -> str:
    """`ff/v1/d/{device_id}/up/{channel}` — what the ingestor subscribes to."""
    return f"{TOPIC_ROOT}/{device_id}/up/{channel}"


def dn_filter(device_id: str) -> str:
    """`ff/v1/d/{device_id}/dn/#` — the only filter the `%u` pattern ACL grants a board."""
    return f"{TOPIC_ROOT}/{device_id}/dn/#"


def will_for(device_id: str) -> aiomqtt.Will:
    """The Last Will: retained `{"online":false}` on `up/presence`, QoS 1.

    Retained on purpose — property 2 in the module docstring. A missing or
    non-retained will is invisible until a board dies, which is why it has its own
    test rather than only being covered by the session test.
    """
    return aiomqtt.Will(
        topic=up_topic(device_id, PRESENCE),
        payload=PRESENCE_OFFLINE,
        qos=QOS,
        retain=True,
    )


@dataclass(frozen=True, slots=True)
class DeviceIdentity:
    """Exactly the `up/announce` identity, and therefore exactly the enroll body.

    Defaults are a plausible connect-only R0 board. `capabilities` is empty because
    R0-fw-1 is connect-only and a simulator that claimed `ota` would be lying about
    what it can do; `agent_version` says `-sim` on purpose, so a fake board is
    identifiable in the fleet list.
    """

    device_id: str
    platform_type: str = "esp32c6"
    fw_version: str = "1.4.2"
    agent_version: str = "0.1.0-sim"
    link_type: str = "wifi"
    power_class: str = "always_on"
    expected_wake_interval_s: int | None = None
    parent_device_id: str | None = None
    partition_layout: str = "ab-4m-v1"
    ota_slot_size: int = 1966080
    capabilities: tuple[str, ...] = ()
    proto: int = 1

    def validate(self) -> None:
        """Refuse an identity the server would refuse — **before any I/O**.

        `POST /v1/enroll` follows the same ordering rule internally ("a burn followed
        by a CHECK violation is a token destroyed by a firmware typo"). Here it is
        stricter than a nicety: a `sleepy` board with no wake interval that reached
        the HTTP call would spend a single-use token to earn a 422.
        """
        if not DEVICE_ID_RE.match(self.device_id):
            raise SimulatorError(
                f"device_id {self.device_id!r} is not 12 lowercase hex digits (the eFuse MAC). "
                "It is never normalised — see fleetforge/identity.py."
            )
        if self.parent_device_id is not None and not DEVICE_ID_RE.match(self.parent_device_id):
            raise SimulatorError(
                f"parent_device_id {self.parent_device_id!r} is not 12 lowercase hex digits"
            )
        if self.power_class not in POWER_CLASSES:
            raise SimulatorError(
                f"power_class must be one of {sorted(POWER_CLASSES)}, not {self.power_class!r}"
            )
        if self.power_class == "sleepy" and not (self.expected_wake_interval_s or 0) > 0:
            raise SimulatorError(
                "power_class=sleepy requires a positive --wake-interval: without it "
                "2.5 x expected_wake_interval_s is undefined and the board would never "
                "read offline. Refused before the enrollment token is presented."
            )

    def announce(self) -> dict[str, object]:
        """The `up/announce` body, key for key as `spec/device-protocol.md` prints it.

        **No `ts` field**, here or anywhere: *"the server timestamps by its own receipt
        time"*.
        """
        return {
            "proto": self.proto,
            "device_id": self.device_id,
            "platform_type": self.platform_type,
            "fw_version": self.fw_version,
            "agent_version": self.agent_version,
            "link_type": self.link_type,
            "power_class": self.power_class,
            "expected_wake_interval_s": self.expected_wake_interval_s,
            "parent_device_id": self.parent_device_id,
            "partition_layout": self.partition_layout,
            "ota_slot_size": self.ota_slot_size,
            "capabilities": list(self.capabilities),
        }

    def enroll_body(self, token: str) -> dict[str, object]:
        """`{ token, <the announce identity payload> }` — **flat, and it stays flat**.

        `spec/device-protocol.md` step 2 and `api/schemas.py::EnrollRequest` both say
        flat. A nested `{"token": …, "identity": {…}}` body is a recall, because the
        R0 agent is flash-baked and speaks whatever it was born with.
        """
        return {"token": token, **self.announce()}

    def heartbeat(self, uptime_s: int) -> dict[str, object]:
        """`up/hb`, verbatim from the spec. The health fields are R3 and are constants."""
        return {
            "fw_version": self.fw_version,
            "uptime_s": uptime_s,
            "rssi": -61,
            "free_heap": 142000,
            "boot_ok": True,
        }


def encode(payload: dict[str, object]) -> bytes:
    """Compact UTF-8 JSON. Flat and small, so CBOR stays a drop-in for V3."""
    return json.dumps(payload, separators=(",", ":")).encode("utf-8")


@dataclass(slots=True)
class LinkProfile:
    """A deterministic bad link: seeded latency before every publish.

    `fast` adds nothing. `slow` draws each delay from `[0.5, 3.0] s` out of a
    `random.Random(seed)`, so a run is reproducible — the same seed produces the same
    delay sequence, which is what makes "the board stayed online through a bad link" a
    repeatable observation rather than an anecdote. The enroll POST gets an extra
    `SLOW_ENROLL_EXTRA_S` on top, because first contact over a marginal link is the
    request most likely to time out.

    It never drops a message. QoS 1 over a persistent session does not lose them.
    """

    name: str = LINK_FAST
    seed: int = 0
    # Not compared: two profiles with the same seed hold two distinct Random objects,
    # and equality on a link profile means "same name, same seed".
    _random: random.Random = field(init=False, repr=False, compare=False)

    def __post_init__(self) -> None:
        if self.name not in LINK_PROFILES:
            raise SimulatorError(f"--link must be one of {list(LINK_PROFILES)}, not {self.name!r}")
        self._random = random.Random(self.seed)  # noqa: S311 - jitter, not a credential

    @property
    def is_slow(self) -> bool:
        return self.name == LINK_SLOW

    def next_delay(self) -> float:
        """The delay to apply before the next publish, in seconds."""
        if not self.is_slow:
            return 0.0
        return self._random.uniform(SLOW_MIN_DELAY_S, SLOW_MAX_DELAY_S)  # noqa: S311 - see above

    def enroll_delay(self) -> float:
        """The delay around the enroll POST — one draw plus the first-contact penalty."""
        if not self.is_slow:
            return 0.0
        return self.next_delay() + SLOW_ENROLL_EXTRA_S


async def _paced_publish(
    client: aiomqtt.Client,
    link: LinkProfile,
    topic: str,
    payload: bytes,
    *,
    retain: bool,
    step: Step,
) -> None:
    """Publish one QoS-1 message, after the link profile's delay.

    The retain flag is printed, because it is the part of the contract a reader cannot
    otherwise see and the part that breaks silently when it is wrong.
    """
    delay = link.next_delay()
    if delay:
        step(f"link     +{delay:.2f}s of latency before {topic}")
        await asyncio.sleep(delay)
    await client.publish(topic, payload, qos=QOS, retain=retain)
    step(f"publish  {topic} (qos {QOS}, {'retain' if retain else 'no retain'})")


async def _heartbeat_loop(
    client: aiomqtt.Client,
    device: DeviceIdentity,
    link: LinkProfile,
    interval_s: float,
    boot_monotonic: float,
    step: Step,
) -> None:
    """One `up/hb` every `interval_s`, starting immediately. **Never retained.**"""
    while True:
        uptime_s = int(time.monotonic() - boot_monotonic)
        await _paced_publish(
            client,
            link,
            up_topic(device.device_id, HEARTBEAT),
            encode(device.heartbeat(uptime_s)),
            retain=False,
            step=step,
        )
        await asyncio.sleep(interval_s)


async def _command_loop(client: aiomqtt.Client, step: Step) -> None:
    """Drain `dn/#`, deduplicate on `id`, and log. Nothing else — R0-fw-1 is connect-only.

    Executing a `stage` would mean inventing the `up/status` state machine R1/R2 has
    not designed yet. Dedup is real, though: QoS 1 is at-least-once and
    `spec/device-protocol.md` makes deduplication the *device's* job.
    """
    seen: set[str] = set()
    async for message in client.messages:
        raw = message.payload if isinstance(message.payload, bytes | bytearray) else b""
        try:
            body = json.loads(bytes(raw))
        except (json.JSONDecodeError, UnicodeDecodeError):
            step(f"command  {message.topic.value} ({len(raw)} bytes, undecodable) — ignored")
            continue
        command_id = body.get("id") if isinstance(body, dict) else None
        if isinstance(command_id, str) and command_id in seen:
            step(f"command  {message.topic.value} id={command_id} DUPLICATE — dropped")
            continue
        if isinstance(command_id, str):
            seen.add(command_id)
        kind = body.get("type") if isinstance(body, dict) else None
        step(
            f"command  {message.topic.value} id={command_id} type={kind} — logged, "
            "not executed (R0 is connect-only)"
        )


async def run_session(
    device: DeviceIdentity,
    credential: Credential,
    *,
    client_factory: ClientFactory,
    link: LinkProfile,
    heartbeat_interval_s: float = DEFAULT_HEARTBEAT_INTERVAL_S,
    awake_s: float | None = None,
    goodbye: bool = True,
    stop: asyncio.Event | None = None,
    connected: asyncio.Event | None = None,
    boot_monotonic: float | None = None,
    endpoint: str = "broker",
    step: Step = _silent,
) -> None:
    """One connected session: subscribe, announce, be present, beat, then leave.

    Ordering is the contract, not a preference:

    1. **subscribe to `dn/#` first**, so a command queued in the persistent session
       while the board was away is drained before anything else happens;
    2. `announce` (retained) — identity, before any claim about liveness;
    3. `presence {"online":true}` (retained);
    4. heartbeats and the command reader, concurrently;
    5. on a clean exit, the **goodbye** — retained `{"online":false}` — and only then
       the DISCONNECT the context manager sends.

    Ends when `stop` is set, when `awake_s` elapses (the sleepy duty cycle), or when a
    child task raises — an `aiomqtt.MqttError` from the heartbeat loop propagates so
    the caller's reconnect logic can see it. `stop` rather than task cancellation is
    deliberate: publishing the goodbye needs a live event loop and an uncancelled
    task, and a `finally:` that awaits inside a cancelled task cannot have one.

    `connected` is set once the CONNECT succeeded. `run_always_on` uses it to tell "the
    coordinates or the credential are wrong" (fatal, first attempt) from "the broker
    blipped" (retriable) — retrying the first kind behind a backoff would hide a typo
    forever.
    """
    stop = stop if stop is not None else asyncio.Event()
    boot_monotonic = boot_monotonic if boot_monotonic is not None else time.monotonic()

    client = client_factory()
    async with client:
        if connected is not None:
            connected.set()
        step(
            f"connect  {endpoint} as {credential.mqtt_username} "
            f"(client_id={device.device_id}, clean_session=False, will=retained "
            f"{PRESENCE_OFFLINE.decode()} on up/presence)"
        )
        await client.subscribe(dn_filter(device.device_id), qos=QOS)
        step(f"subscribe {dn_filter(device.device_id)} (qos {QOS})")

        await _paced_publish(
            client,
            link,
            up_topic(device.device_id, ANNOUNCE),
            encode(device.announce()),
            retain=True,
            step=step,
        )
        await _paced_publish(
            client,
            link,
            up_topic(device.device_id, PRESENCE),
            PRESENCE_ONLINE,
            retain=True,
            step=step,
        )

        workers = [
            asyncio.create_task(
                _heartbeat_loop(client, device, link, heartbeat_interval_s, boot_monotonic, step)
            ),
            asyncio.create_task(_command_loop(client, step)),
        ]
        waiter = asyncio.create_task(stop.wait())
        try:
            done, _ = await asyncio.wait(
                [*workers, waiter], timeout=awake_s, return_when=asyncio.FIRST_COMPLETED
            )
            for task in done:
                if task is not waiter:
                    # Re-raises MqttError from a worker; a worker returning normally
                    # means the broker closed the message stream, which is the same
                    # thing the caller must react to.
                    task.result()
        finally:
            for task in [*workers, waiter]:
                task.cancel()
            await asyncio.gather(*workers, waiter, return_exceptions=True)

        if goodbye:
            # A clean DISCONNECT does not fire the LWT — property 4. Without this the
            # dashboard shows a board that is not running until it comes back.
            await client.publish(
                up_topic(device.device_id, PRESENCE), PRESENCE_OFFLINE, qos=QOS, retain=True
            )
            step(
                f"goodbye  {up_topic(device.device_id, PRESENCE)} "
                f"{PRESENCE_OFFLINE.decode()} (qos {QOS}, retain)"
            )


# Copied from `ingestor/main.py::run`: a board that gives up on the first broker blip
# is not a useful simulator, and neither is one that hammers a down broker.
RECONNECT_INITIAL_DELAY_S = 1.0
RECONNECT_MAX_DELAY_S = 30.0


async def run_always_on(
    device: DeviceIdentity,
    credential: Credential,
    *,
    client_factory: ClientFactory,
    link: LinkProfile,
    heartbeat_interval_s: float,
    stop: asyncio.Event,
    endpoint: str = "broker",
    step: Step = _silent,
) -> None:
    """One session, forever, reconnecting with capped exponential backoff.

    **The first connection is fatal if it fails.** A wrong host, a wrong port or a
    revoked broker credential must read as `SIMULATOR FAILED: MqttError: …` rather than
    as a board quietly retrying every 30 s with nothing in the fleet view; after one
    successful CONNECT, the same error is a broker blip and is retried, because a
    simulator that dies on one is not a useful stand-in for a board.
    """
    boot_monotonic = time.monotonic()
    connected = asyncio.Event()
    delay = RECONNECT_INITIAL_DELAY_S
    while not stop.is_set():
        try:
            await run_session(
                device,
                credential,
                client_factory=client_factory,
                link=link,
                heartbeat_interval_s=heartbeat_interval_s,
                stop=stop,
                connected=connected,
                boot_monotonic=boot_monotonic,
                endpoint=endpoint,
                step=step,
            )
        except (aiomqtt.MqttError, OSError) as exc:
            if not connected.is_set() or stop.is_set():
                raise
            step(f"reconnect broker error ({exc}); retrying in {delay:.0f}s")
        else:
            if stop.is_set():
                return
            step(f"reconnect the broker closed the session; retrying in {delay:.0f}s")
            delay = RECONNECT_INITIAL_DELAY_S
        await _sleep_until(stop, delay)
        delay = min(delay * 2, RECONNECT_MAX_DELAY_S)


async def run_sleepy(
    device: DeviceIdentity,
    credential: Credential,
    *,
    client_factory: ClientFactory,
    link: LinkProfile,
    heartbeat_interval_s: float,
    wake_interval_s: float,
    awake_s: float,
    stop: asyncio.Event,
    endpoint: str = "broker",
    step: Step = _silent,
) -> None:
    """wake → connect → announce/presence/hb → drain `dn/` → disconnect → sleep → repeat.

    **No goodbye between wakes.** For a sleepy board `presence.is_online` ignores
    `presence_reported` entirely and uses `last_seen`, so a `{"online":false}` would be
    noise the server is contractually required to ignore — and the ingestor would drop
    it anyway. This is the only mode that exercises the
    `2.5 x expected_wake_interval_s` branch end to end.
    """
    boot_monotonic = time.monotonic()
    while not stop.is_set():
        step(f"wake     staying up {awake_s:.0f}s")
        await run_session(
            device,
            credential,
            client_factory=client_factory,
            link=link,
            heartbeat_interval_s=heartbeat_interval_s,
            awake_s=awake_s,
            goodbye=False,
            stop=stop,
            boot_monotonic=boot_monotonic,
            endpoint=endpoint,
            step=step,
        )
        if stop.is_set():
            return
        step(f"sleep    {wake_interval_s:.0f}s until the next wake")
        await _sleep_until(stop, wake_interval_s)


async def _sleep_until(stop: asyncio.Event, seconds: float) -> None:
    """Sleep, but wake immediately if the process is shutting down."""
    with contextlib.suppress(TimeoutError):
        await asyncio.wait_for(stop.wait(), timeout=seconds)


def mqtt_client_factory(
    host: str, port: int, credential: Credential, device_id: str, tls: bool = False
) -> ClientFactory:
    """A factory that builds a **fresh** `aiomqtt.Client` per session.

    Fresh, because entering the same client twice raises `MqttReentrantError` and the
    sleepy loop connects once per wake (property 5).

    `tls` is off by default because the DEV broker is plaintext behind Traefik's
    `mqtt` entrypoint. Production (R0-infra-3) terminates real TLS on 8883, so
    `--tls` is mandatory there — without it the handshake never happens and the
    connection just hangs until it times out, which reads like a firewall problem
    rather than a missing flag.

    Verification only, never certificate pinning: `TLSParameters()` with no
    arguments uses the system trust store, which is what validated the Let's
    Encrypt chain for `bingo.tvaroska.sk`. A real ESP32 agent pins differently
    (`spec/device-protocol.md`), and this simulator is not the place to model that.
    """

    def factory() -> aiomqtt.Client:
        return aiomqtt.Client(
            hostname=host,
            port=port,
            identifier=device_id,
            username=credential.mqtt_username,
            password=credential.mqtt_password,
            will=will_for(device_id),
            clean_session=False,
            keepalive=KEEPALIVE_S,
            tls_params=aiomqtt.TLSParameters() if tls else None,
        )

    return factory

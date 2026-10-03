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
import urllib.parse
import urllib.request
from collections.abc import Callable
from dataclasses import dataclass, field, replace

import aiomqtt

from fleetforge.identity import DEVICE_ID_RE
from fleetforge.simulator.errors import SimulatorError
from fleetforge.simulator.state import Credential

# `spec/device-protocol.md` → *Topic namespace*. Spelled once, here.
TOPIC_ROOT = "ff/v1/d"

ANNOUNCE = "announce"
PRESENCE = "presence"
HEARTBEAT = "hb"
STATUS = "status"
# `dn/cmd`, the only downlink channel in R1. Retyped here rather than imported from
# `broker/commands.py` for the reason `POWER_CLASSES` is retyped — see below.
COMMAND = "cmd"

# `spec/device-protocol.md` → *`up/status` — the update transaction*, retyped for the
# same reason as `POWER_CLASSES`: importing `fleetforge.db` would drag SQLAlchemy into a
# process pretending to be an ESP32. `tests/test_simulator.py` asserts these agree with
# `db/models.py::DeployState` — the tripwire, not a hope.
STATE_STAGING = "staging"
STATE_DOWNLOADING = "downloading"
STATE_VERIFYING = "verifying"
STATE_STAGED = "staged"
STATE_AWAITING_SAFE_WINDOW = "awaiting_safe_window"
STATE_APPLYING = "applying"
STATE_REBOOTING = "rebooting"
STATE_FAILED = "failed"
# The outcome (R2-be-1), reported by the session that observed it — never by `_stage`.
STATE_CONFIRMING = "confirming"
STATE_CONFIRMED = "confirmed"
STATE_ROLLING_BACK = "rolling_back"
STATE_ROLLED_BACK = "rolled_back"

# The walk a healthy `apply: "auto"` deploy makes up to the reboot. `awaiting_safe_window`
# is NOT in it: a board that judges the window safe immediately goes straight from
# `staged` to `applying` (`--safe-window hold` is the other case, and it never leaves).
# What follows the reboot is reported by the NEXT session (`StageRunner.on_boot`):
# `CONFIRM_WALK` for an image that confirms, `ROLLBACK_WALK` for one that does not.
STAGE_WALK = (
    STATE_STAGING,
    STATE_DOWNLOADING,
    STATE_VERIFYING,
    STATE_STAGED,
    STATE_APPLYING,
    STATE_REBOOTING,
)
CONFIRM_WALK = (STATE_CONFIRMING, STATE_CONFIRMED)
ROLLBACK_WALK = (STATE_CONFIRMING, STATE_ROLLING_BACK, STATE_ROLLED_BACK)

# `spec/device-protocol.md` → *`dn/cmd` — commands*: the type this simulator executes,
# and the two `apply` modes.
COMMAND_STAGE = "stage"
APPLY_AUTO = "auto"
APPLY_ON_COMMAND = "on_command"

# `--safe-window`: whether this board ever judges the moment safe. `hold` parks in
# `awaiting_safe_window` forever, which is how "the server never expires it" becomes an
# observable property rather than a claim (`design/architecture.md` principle 5).
SAFE_WINDOW_AUTO = "auto"
SAFE_WINDOW_HOLD = "hold"
SAFE_WINDOW_MODES = (SAFE_WINDOW_AUTO, SAFE_WINDOW_HOLD)

# `--confirm`: what the image this board reboots into does. `auto` confirms at the
# announce PUBACK, like a healthy agent. `never` joins the fleet and never confirms — an
# `FF_ROLLBACK_TEST` image (`docs/runbooks/rollback-test.md`) — so the confirm timer
# fires and the board goes back to the version it came from.
CONFIRM_AUTO = "auto"
CONFIRM_NEVER = "never"
CONFIRM_MODES = (CONFIRM_AUTO, CONFIRM_NEVER)
# `spec/prd.md` → *Requirements & targets → Timing*: "confirm timeout 300 s". The real
# agent ignores the command's `confirm_timeout_s` and so does this one.
DEFAULT_CONFIRM_TIMEOUT_S = 300.0

# Artifact downloads are chunked so a 1.9 MB image does not arrive as one read, the same
# shape the real agent's HTTPS client uses.
DOWNLOAD_CHUNK_BYTES = 64 * 1024
DOWNLOAD_TIMEOUT_S = 30.0

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


def redact_url(url: str) -> str:
    """`https://host/…` — scheme and host only.

    A signed artifact URL is a **bearer credential**: the signature is the
    authorization (`spec/prd.md` → *Security & data posture*), so the query string is
    the secret. The transcript is the only place a human can see a `dn/` payload at all
    — by design no credential may subscribe to `dn/#` — so it prints the shape of the
    URL and never the URL.
    """
    parts = urllib.parse.urlsplit(url)
    if not parts.scheme or not parts.netloc:
        return "<unparseable url>"
    return f"{parts.scheme}://{parts.netloc}/…"


def redacted_command(body: dict[str, object]) -> dict[str, object]:
    """`body` with `artifact.url` redacted, ready to print. Never mutates the original."""
    artifact = body.get("artifact")
    if not isinstance(artifact, dict) or not isinstance(artifact.get("url"), str):
        return body
    return {**body, "artifact": {**artifact, "url": redact_url(str(artifact["url"]))}}


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
        """`up/hb`, verbatim from the spec. The health fields are R4 and are constants."""
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


def _fetch(url: str) -> bytes:
    """GET `url` into memory, chunked. **Blocking** — always called via `to_thread`.

    `urllib.request` and never httpx: this module ships in the production image and
    httpx is a dev dependency, which `tests/test_simulator.py`'s AST tripwire enforces.
    An ESP32 streams into the OTA partition instead of buffering; a 1.9 MB cap
    (`spec/prd.md` → *Capacity*) makes buffering fine here and keeps the sha256 check
    trivially correct.
    """
    chunks: list[bytes] = []
    # The URL comes from the server's own `stage` command, over an authenticated broker
    # connection — not from user input.
    with urllib.request.urlopen(url, timeout=DOWNLOAD_TIMEOUT_S) as response:  # noqa: S310
        while True:
            chunk = response.read(DOWNLOAD_CHUNK_BYTES)
            if not chunk:
                break
            chunks.append(chunk)
    return b"".join(chunks)


@dataclass(frozen=True, slots=True)
class PendingConfirm:
    """The NVS analogue: the transaction that crossed the apply reboot (`agent/main/ff_txn.h`).

    `previous_identity` is the board as it was before the apply — what a rollback returns
    it to, captured **before** `fw_version` was rebound.
    """

    cmd_id: str
    previous_identity: DeviceIdentity


@dataclass(slots=True)
class StageRunner:
    """Executes `stage` commands and owns everything that outlives one session.

    The state, each piece for a reason:

    * `seen` — the dedup set. QoS 1 is at-least-once and `spec/device-protocol.md` makes
      deduplication **the device's job**; it lives here rather than in the session loop
      so a command redelivered from the persistent session after a reconnect is still a
      duplicate.
    * `downloads` — counted and printed, because "a duplicated publish produces one
      download" is an acceptance criterion and needs to be observable.
    * `identity` — adopted after a successful apply, so later `up/announce` publishes
      carry the **new** `fw_version`, exactly as a rebooted board would.
    * `reboot` — set when an apply (or a rollback) has happened, and the only way the
      new `identity` ever reaches the server. See `_stage` and `run_session`.
    * `pending` — the transaction an apply rebooted into, set before `reboot`; the next
      session reports its outcome (`on_boot`). The firmware keeps the same thing in NVS,
      because `cmd_id` would otherwise die with the image that received the `stage`.
    * `rolled_back` — the `cmd_id` a rollback still owes a `rolled_back` for. Reported by
      the session on the image the board **returned to**, exactly as on metal.
    """

    identity: DeviceIdentity
    link: LinkProfile
    safe_window: str = SAFE_WINDOW_AUTO
    confirm: str = CONFIRM_AUTO
    confirm_timeout_s: float = DEFAULT_CONFIRM_TIMEOUT_S
    seen: set[str] = field(default_factory=set)
    downloads: int = 0
    # Not arguments: a caller passing in a pre-set event would start a board that reboots
    # before it has applied anything, and a pre-set transaction is one nobody issued.
    reboot: asyncio.Event = field(default_factory=asyncio.Event, init=False)
    pending: PendingConfirm | None = field(default=None, init=False)
    rolled_back: str | None = field(default=None, init=False)
    # When the confirm timer of the current boot fires (monotonic). Armed once per boot,
    # like the firmware's esp_timer: a reconnect resumes it rather than restarting it.
    _confirm_deadline: float | None = field(default=None, init=False, repr=False)

    def __post_init__(self) -> None:
        if self.safe_window not in SAFE_WINDOW_MODES:
            raise SimulatorError(
                f"--safe-window must be one of {list(SAFE_WINDOW_MODES)}, not {self.safe_window!r}"
            )
        if self.confirm not in CONFIRM_MODES:
            raise SimulatorError(
                f"--confirm must be one of {list(CONFIRM_MODES)}, not {self.confirm!r}"
            )
        if not self.confirm_timeout_s > 0:
            raise SimulatorError(
                f"--confirm-timeout must be a positive number of seconds, "
                f"not {self.confirm_timeout_s!r}"
            )

    async def handle(self, client: aiomqtt.Client, message: aiomqtt.Message, step: Step) -> None:
        """Dispatch one `dn/` message: decode, dedup on `id`, execute or log."""
        raw = message.payload if isinstance(message.payload, bytes | bytearray) else b""
        try:
            body = json.loads(bytes(raw))
        except (json.JSONDecodeError, UnicodeDecodeError):
            step(f"command  {message.topic.value} ({len(raw)} bytes, undecodable) — ignored")
            return
        if not isinstance(body, dict):
            step(f"command  {message.topic.value} is not a JSON object — ignored")
            return

        command_id = body.get("id")
        if isinstance(command_id, str) and command_id in self.seen:
            step(f"command  {message.topic.value} id={command_id} DUPLICATE — dropped")
            return
        if isinstance(command_id, str):
            self.seen.add(command_id)

        kind = body.get("type")
        step(f"command  {message.topic.value} id={command_id} type={kind}")
        # The whole payload, with the signed URL redacted: nothing else in the estate can
        # observe a `dn/` message (no credential may subscribe there), so this transcript
        # line is the only way a human checks the wire shape.
        # `ensure_ascii=False` so the redaction's ellipsis prints as `…` and not as an
        # escape — this line exists to be read by a human.
        step(f"payload  {json.dumps(redacted_command(body), sort_keys=True, ensure_ascii=False)}")

        if kind != COMMAND_STAGE:
            step(f"command  type={kind} is not implemented by this simulator — ignored")
            return
        if not isinstance(command_id, str) or not command_id:
            step("command  a stage with no id cannot be deduplicated — ignored")
            return
        await self._stage(client, command_id, body, step)

    async def _stage(
        self, client: aiomqtt.Client, cmd_id: str, body: dict[str, object], step: Step
    ) -> None:
        """The state walk: stage → download → verify → stage(d) → apply → reboot.

        It ends at `rebooting`, as the firmware's does: the outcome belongs to the next
        session (`on_boot`), which is the one that can observe it.
        """
        artifact = body.get("artifact")
        if not isinstance(artifact, dict):
            await self._status(client, cmd_id, STATE_FAILED, step, detail="no artifact in command")
            return
        url = artifact.get("url")
        expected_sha = artifact.get("sha256")
        version = artifact.get("version")
        if not isinstance(url, str) or not isinstance(expected_sha, str):
            await self._status(
                client, cmd_id, STATE_FAILED, step, detail="artifact url/sha256 missing"
            )
            return

        await self._status(client, cmd_id, STATE_STAGING, step)

        await self._status(client, cmd_id, STATE_DOWNLOADING, step)
        try:
            data = await asyncio.to_thread(_fetch, url)
        except OSError as exc:
            # A real board retries; this one reports and stops, because a silent retry
            # loop in a transcript is indistinguishable from a hang.
            await self._status(
                client, cmd_id, STATE_FAILED, step, detail=f"download failed: {type(exc).__name__}"
            )
            return
        self.downloads += 1
        step(f"download {len(data)} bytes from {redact_url(url)} (download #{self.downloads})")

        await self._status(client, cmd_id, STATE_VERIFYING, step)
        digest = hashlib.sha256(data).hexdigest()
        if digest != expected_sha:
            # Unverified bytes are never applied. The mismatch is printed in full: both
            # digests are public facts about the artifact, unlike the URL.
            step(f"verify   MISMATCH got {digest}, expected {expected_sha}")
            await self._status(client, cmd_id, STATE_FAILED, step, detail="sha256 mismatch")
            return
        step(f"verify   sha256 {digest} matches")

        await self._status(client, cmd_id, STATE_STAGED, step)

        if body.get("apply") == APPLY_ON_COMMAND:
            # `spec/device-protocol.md`: the device stages and waits for an explicit
            # `apply`. R1 ships no such command, so this board sits here until R2.
            step("stage    apply=on_command — staying staged until an apply arrives (R2)")
            return

        if self.safe_window == SAFE_WINDOW_HOLD:
            # Parked, permanently and on purpose. Nothing server-side may end this:
            # a vehicle in motion reports honestly for as long as it is moving.
            await self._status(client, cmd_id, STATE_AWAITING_SAFE_WINDOW, step)
            step("stage    --safe-window hold: parked in awaiting_safe_window, indefinitely")
            return

        await self._status(client, cmd_id, STATE_APPLYING, step)
        await self._status(client, cmd_id, STATE_REBOOTING, step)
        # The transaction crosses the reboot (the firmware writes it to NVS at `staged`;
        # nothing here can reset between the two, so recording it at the reboot is the
        # same). `previous_identity` is taken BEFORE the rebind below: it is what a
        # rollback returns the board to.
        self.pending = PendingConfirm(cmd_id=cmd_id, previous_identity=self.identity)
        self._confirm_deadline = None
        if isinstance(version, str) and version:
            # The board comes back running the new image: later announces and heartbeats
            # say so, which is what makes "delivery success" checkable end to end.
            self.identity = replace(self.identity, fw_version=version)
            step(f"apply    now running fw_version {version} (announced on the next connect)")
        # **And the next connect has to actually happen.** Rebinding `identity` above
        # changes nothing the server can see: `run_session` captured the old frozen
        # identity and its heartbeat loop keeps publishing from it, so a board that never
        # drops its session never reports the version it just applied (S0-test-4 — the
        # defect that made CUJ-1 segment 5 unpassable). A real board reboots here, which
        # is what ends the session; this is the simulator's reboot. Unconditional,
        # because the reboot is the consequence of the apply and not of the payload
        # carrying a version.
        self.reboot.set()

    async def on_boot(self, client: aiomqtt.Client, step: Step) -> "asyncio.Task[None] | None":
        """Report what the last reboot left owed. Called after the announce + presence.

        Those two publishes are awaited at QoS 1, so returning from them is this
        simulator's announce PUBACK — the moment the firmware confirms. Each outcome is
        cleared only after its publish returned, the firmware's clear-at-PUBACK: a
        session that dies first leaves it owed, and the next one says it again (the
        server deduplicates on `(cmd_id, state)`).

        Returns the confirm-timer task for a `--confirm never` image, which the session
        must treat as a sentinel: it ends the session by setting `reboot`.
        """
        if self.rolled_back is not None:
            await self._status(
                client,
                self.rolled_back,
                STATE_ROLLED_BACK,
                step,
                detail=f"returned to {self.identity.fw_version}; the new image did not confirm",
            )
            self.rolled_back = None
            return None

        pending = self.pending
        if pending is None:
            return None
        await self._status(client, pending.cmd_id, STATE_CONFIRMING, step)
        if self.confirm == CONFIRM_AUTO:
            await self._status(client, pending.cmd_id, STATE_CONFIRMED, step, pct=100)
            step(f"confirm  {self.identity.fw_version} confirmed at the announce ack")
            self.pending = None
            self._confirm_deadline = None
            return None

        if self._confirm_deadline is None:
            self._confirm_deadline = time.monotonic() + self.confirm_timeout_s
        remaining = max(0.0, self._confirm_deadline - time.monotonic())
        step(f"confirm  --confirm never: rolling back in {remaining:.1f}s unless confirmed")
        return asyncio.create_task(self._confirm_timeout(client, pending, remaining, step))

    async def _confirm_timeout(
        self, client: aiomqtt.Client, pending: PendingConfirm, delay_s: float, step: Step
    ) -> None:
        """`ff_mqtt.c::confirm_timeout_cb`: report `rolling_back` (best-effort), then go back.

        The report is attempted BEFORE the reboot is triggered, because setting `reboot`
        ends the session and cancels this task. It is best-effort exactly as on metal:
        a failed publish is logged and the rollback happens anyway.
        """
        await asyncio.sleep(delay_s)
        step(
            f"confirm  no confirm {self.confirm_timeout_s:g}s after boot — rolling back to "
            f"{pending.previous_identity.fw_version}"
        )
        try:
            await self._status(client, pending.cmd_id, STATE_ROLLING_BACK, step)
        except aiomqtt.MqttError as exc:
            step(f"confirm  {STATE_ROLLING_BACK} not delivered ({exc}); rolling back anyway")
        self.identity = pending.previous_identity
        self.rolled_back = pending.cmd_id
        self.pending = None
        self._confirm_deadline = None
        self.reboot.set()

    async def _status(
        self,
        client: aiomqtt.Client,
        cmd_id: str,
        state: str,
        step: Step,
        *,
        pct: int | None = None,
        detail: str | None = None,
    ) -> None:
        """One `up/status`, QoS 1 and **retained**.

        Retained because `spec/device-protocol.md` says so: the current state of the
        update transaction must survive the server restarting, or a board parked in
        `awaiting_safe_window` becomes invisible to a dashboard that came up after it.
        """
        payload: dict[str, object] = {
            "cmd_id": cmd_id,
            "state": state,
            "pct": pct,
            "detail": detail,
        }
        await _paced_publish(
            client,
            self.link,
            up_topic(self.identity.device_id, STATUS),
            encode(payload),
            retain=True,
            step=step,
        )
        step(f"status   {state}")


async def _command_loop(client: aiomqtt.Client, stage: StageRunner, step: Step) -> None:
    """Drain `dn/#` and hand every message to the stage runner.

    R0 logged commands and executed nothing; since R1-be-2 a `stage` is really carried
    out — downloaded, verified against its sha256 and "applied" — so the acceptance
    criterion ("the device fetched the bytes") is observable rather than asserted.
    Commands of any other type are still logged and ignored: `apply`, `cancel`,
    `rollback`, `identify`, `reboot` and `set_cfg` are later releases.
    """
    async for message in client.messages:
        await stage.handle(client, message, step)


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
    stage: StageRunner | None = None,
    step: Step = _silent,
) -> None:
    """One connected session: subscribe, announce, be present, beat, then leave.

    Ordering is the contract, not a preference:

    1. **subscribe to `dn/#` first**, so a command queued in the persistent session
       while the board was away is drained before anything else happens;
    2. `announce` (retained) — identity, before any claim about liveness;
    3. `presence {"online":true}` (retained);
    4. whatever the last reboot left owed — `confirming`/`confirmed`, or `rolled_back`
       (`StageRunner.on_boot`);
    5. heartbeats and the command reader, concurrently;
    6. on a clean exit, the **goodbye** — retained `{"online":false}` — and only then
       the DISCONNECT the context manager sends.

    Ends when `stop` is set, when `stage.reboot` is set (an apply happened, or a
    `--confirm never` image's confirm timer fired — the session dies the way a restarting
    board's does, and the caller's loop brings it back on the right image), when `awake_s`
    elapses (the sleepy duty cycle), or when a child task
    raises — an `aiomqtt.MqttError` from the heartbeat loop propagates so the caller's
    reconnect logic can see it. `stop` rather than task cancellation is
    deliberate: publishing the goodbye needs a live event loop and an uncancelled
    task, and a `finally:` that awaits inside a cancelled task cannot have one.

    `connected` is set once the CONNECT succeeded. `run_always_on` uses it to tell "the
    coordinates or the credential are wrong" (fatal, first attempt) from "the broker
    blipped" (retriable) — retrying the first kind behind a backoff would hide a typo
    forever.

    `stage` outlives the session: its dedup set must survive a reconnect, because a QoS-1
    command redelivered from the persistent session is a duplicate, not a new deploy. The
    callers below own one per board and pass `stage.identity` as `device`, which is how a
    board that applied an update announces the **new** `fw_version` on its next connect.
    """
    stop = stop if stop is not None else asyncio.Event()
    boot_monotonic = boot_monotonic if boot_monotonic is not None else time.monotonic()
    stage = stage if stage is not None else StageRunner(identity=device, link=link)

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
        # After the announce, so the server knows which version is running when the
        # outcome of the last reboot lands.
        confirm_timer = await stage.on_boot(client, step)

        workers = [
            asyncio.create_task(
                _heartbeat_loop(client, device, link, heartbeat_interval_s, boot_monotonic, step)
            ),
            asyncio.create_task(_command_loop(client, stage, step)),
        ]
        # Two ways to be asked to leave, and they are not the same event: `stop` is the
        # operator, `stage.reboot` is the board restarting into the image it just applied
        # (or rolling back from it). The confirm timer is a sentinel too, NOT a worker: it
        # returns normally after setting `reboot`, and a worker returning normally means
        # "the broker closed the stream".
        sentinels: list[asyncio.Task[object]] = [
            asyncio.create_task(stop.wait()),
            asyncio.create_task(stage.reboot.wait()),
        ]
        if confirm_timer is not None:
            sentinels.append(confirm_timer)
        try:
            done, _ = await asyncio.wait(
                [*workers, *sentinels], timeout=awake_s, return_when=asyncio.FIRST_COMPLETED
            )
            for task in done:
                if task is confirm_timer:
                    task.result()  # a bug in the timer surfaces; a normal return is a reboot
                elif task not in sentinels:
                    # Re-raises MqttError from a worker; a worker returning normally
                    # means the broker closed the message stream, which is the same
                    # thing the caller must react to.
                    task.result()
        finally:
            for task in [*workers, *sentinels]:
                task.cancel()
            await asyncio.gather(*workers, *sentinels, return_exceptions=True)

        if stage.reboot.is_set():
            # A rebooting board says nothing on its way out — it is gone mid-sentence.
            # The one thing this cannot reproduce is the ungraceful drop: `aiomqtt` has
            # no public API for it (property 4), so the context manager below still sends
            # a DISCONNECT and the LWT does not fire. Server-visible difference is that
            # retained presence stays `{"online":true}` across the reboot rather than
            # flapping offline for the length of a boot, which is the benign direction.
            step(f"reboot   restarting into fw_version {stage.identity.fw_version}")
        elif goodbye:
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

# How long the simulated board is "down" between an apply and the session that announces
# the new version. It stands in for a real ESP32's restart — bootloader, app start, Wi-Fi
# association, broker CONNECT — which `R1-test-1` measured at a few seconds on metal. Not
# part of the backoff: a reboot is a scheduled absence, not a broker problem, so it
# neither grows nor resets the reconnect delay.
REBOOT_DELAY_S = 2.0


async def run_always_on(
    device: DeviceIdentity,
    credential: Credential,
    *,
    client_factory: ClientFactory,
    link: LinkProfile,
    heartbeat_interval_s: float,
    stop: asyncio.Event,
    endpoint: str = "broker",
    safe_window: str = SAFE_WINDOW_AUTO,
    confirm: str = CONFIRM_AUTO,
    confirm_timeout_s: float = DEFAULT_CONFIRM_TIMEOUT_S,
    step: Step = _silent,
) -> None:
    """One session, forever, reconnecting with capped exponential backoff.

    **The first connection is fatal if it fails.** A wrong host, a wrong port or a
    revoked broker credential must read as `SIMULATOR FAILED: MqttError: …` rather than
    as a board quietly retrying every 30 s with nothing in the fleet view; after one
    successful CONNECT, the same error is a broker blip and is retried, because a
    simulator that dies on one is not a useful stand-in for a board.

    The `StageRunner` is built **here**, outside the reconnect loop, so dedup and the
    applied firmware version survive a reconnect.

    **A reboot is a third kind of session end**, distinct from the two in the paragraph
    above: not an error and not a broker closing the stream, but the board deliberately
    restarting into an image it just applied. It is the only path by which a new
    `fw_version` reaches the server, so it is handled here rather than swept into the
    generic reconnect — see `REBOOT_DELAY_S`.
    """
    boot_monotonic = time.monotonic()
    connected = asyncio.Event()
    stage = StageRunner(
        identity=device,
        link=link,
        safe_window=safe_window,
        confirm=confirm,
        confirm_timeout_s=confirm_timeout_s,
    )
    delay = RECONNECT_INITIAL_DELAY_S
    while not stop.is_set():
        try:
            await run_session(
                stage.identity,
                credential,
                client_factory=client_factory,
                link=link,
                heartbeat_interval_s=heartbeat_interval_s,
                stop=stop,
                connected=connected,
                boot_monotonic=boot_monotonic,
                endpoint=endpoint,
                stage=stage,
                step=step,
            )
        except (aiomqtt.MqttError, OSError) as exc:
            if not connected.is_set() or stop.is_set():
                raise
            step(f"reconnect broker error ({exc}); retrying in {delay:.0f}s")
        else:
            if stop.is_set():
                return
            if stage.reboot.is_set():
                # The session ended because the board applied an update. Come back as a
                # freshly booted board: uptime restarts, and the next `run_session` is
                # handed `stage.identity`, so the announce and every heartbeat after it
                # carry the version that is now running.
                stage.reboot.clear()
                boot_monotonic = time.monotonic()
                step(f"boot     back in {REBOOT_DELAY_S:.0f}s on {stage.identity.fw_version}")
                await _sleep_until(stop, REBOOT_DELAY_S)
                delay = RECONNECT_INITIAL_DELAY_S
                continue
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
    safe_window: str = SAFE_WINDOW_AUTO,
    confirm: str = CONFIRM_AUTO,
    confirm_timeout_s: float = DEFAULT_CONFIRM_TIMEOUT_S,
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
    stage = StageRunner(
        identity=device,
        link=link,
        safe_window=safe_window,
        confirm=confirm,
        confirm_timeout_s=confirm_timeout_s,
    )
    while not stop.is_set():
        step(f"wake     staying up {awake_s:.0f}s")
        await run_session(
            stage.identity,
            credential,
            client_factory=client_factory,
            link=link,
            heartbeat_interval_s=heartbeat_interval_s,
            awake_s=awake_s,
            goodbye=False,
            stop=stop,
            boot_monotonic=boot_monotonic,
            endpoint=endpoint,
            stage=stage,
            step=step,
        )
        if stop.is_set():
            return
        if stage.reboot.is_set():
            # An apply cut the wake short. A sleepy board boots into the new image and
            # then goes back to its duty cycle, so it re-announces on its next wake
            # rather than immediately — the same convergence, one wake interval later.
            stage.reboot.clear()
            boot_monotonic = time.monotonic()
            step(f"boot     rebooted onto {stage.identity.fw_version}")
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

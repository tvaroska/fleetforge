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
import base64
import binascii
import contextlib
import hashlib
import json
import random
import re
import time
import urllib.parse
import urllib.request
from collections.abc import Callable
from dataclasses import dataclass, field, replace
from pathlib import Path

import aiomqtt
from cryptography.exceptions import InvalidSignature
from cryptography.hazmat.primitives import hashes, serialization
from cryptography.hazmat.primitives.asymmetric import ec, utils

from fleetforge.identity import DEVICE_ID_RE
from fleetforge.simulator.errors import SimulatorError
from fleetforge.simulator.state import Credential
from fleetforge.simulator.telemetry import TELEMETRY_FLUSH_S, TELEMETRY_QOS, TelemetryBuffer

# `spec/device-protocol.md` → *Topic namespace*. Spelled once, here.
TOPIC_ROOT = "ff/v1/d"

ANNOUNCE = "announce"
PRESENCE = "presence"
HEARTBEAT = "hb"
STATUS = "status"
# R4-fw-2: QoS 0 and not retained (`telemetry.py`), and off unless --telemetry-every is given,
# so the QoS-1 matrix of every other publish is untouched by default.
TELEMETRY = "telemetry"
# `dn/cmd`, the only downlink channel. Retyped here rather than imported from
# `broker/commands.py` for the reason `POWER_CLASSES` is retyped — see below.
COMMAND = "cmd"
# R5-spec-1 Patch A5: a verb's answer. QoS 1 and **never retained** (an answer is an
# event, not state), so it goes through `_paced_publish(..., retain=False)`.
ACK = "ack"

# `artifact.sha256` as the firmware accepts it (`agent/components/fleetforge/src/ff_mqtt.c::on_stage`,
# `is_lowercase_sha256`): exactly 64 lowercase hex characters, never normalised. Local on
# purpose, not `fleetforge.storage.blobs.SHA256_HEX`: the simulator imports nothing from
# the server (`tests/test_invariants.py`).
_SHA256_HEX = re.compile(r"[0-9a-f]{64}")

# R6-SEC-1 / R6-SEC-2: `artifact.sig` (`ecdsa-p256-sha256:<base64_der>`) and the 2-key
# public key rotation ring, retyped from `ff_ota.c` rather than imported from
# `fleetforge.signing` so the simulator's import purity (`tests/test_simulator.py`) holds.
_SIG_PREFIX = "ecdsa-p256-sha256:"
_SIG_RE = re.compile(r"\Aecdsa-p256-sha256:[A-Za-z0-9+/]{80,120}={0,2}\Z")
SIGNING_PUBKEY_PRIMARY_PEM = (
    "-----BEGIN PUBLIC KEY-----\n"
    "MFkwEwYHKoZIzj0CAQYIKoZIzj0DAQcDQgAEbHg1tVC28a9y8SbZY0jis7OKKWxr\n"
    "X2+S5g0No7NMyV8nI+vUHHQ8MNQnNSDSPESUAT9m2iLyNC/nM26RvxYV1Q==\n"
    "-----END PUBLIC KEY-----\n"
)
SIGNING_PUBKEY_SECONDARY_PEM = (
    "-----BEGIN PUBLIC KEY-----\n"
    "MFkwEwYHKoZIzj0CAQYIKoZIzj0DAQcDQgAEwJct9NbOiOIbnB/ToYTvq02EBrzs\n"
    "UAETxsFAAWHFWhxfpL3x0OcIH6yW9q6EhYA3q6vNGIEwC7KPJQXd1ONEqQ==\n"
    "-----END PUBLIC KEY-----\n"
)
DEFAULT_SIGNING_PUBKEY_RING: tuple[str, ...] = (
    SIGNING_PUBKEY_PRIMARY_PEM,
    SIGNING_PUBKEY_SECONDARY_PEM,
)


def _is_valid_artifact_sig(sig: object) -> bool:
    """`ff_mqtt.c::is_valid_artifact_sig`: `ecdsa-p256-sha256:<base64_der>` with DER tag 0x30."""
    if not isinstance(sig, str) or len(sig) > 159 or not _SIG_RE.match(sig):
        return False
    try:
        der = base64.b64decode(sig.removeprefix(_SIG_PREFIX), validate=True)
    except binascii.Error:
        return False
    return 8 <= len(der) <= 80 and der[0] == 0x30


def _verify_artifact_sig(
    sha256_hex: str,
    sig: str,
    pubkey_ring: tuple[str, ...] = DEFAULT_SIGNING_PUBKEY_RING,
) -> bool:
    """`ff_ota.c::verify_artifact_signature`: verify DER ECDSA P-256 over the 32-byte digest."""
    if not _is_valid_artifact_sig(sig):
        return False
    der_sig = base64.b64decode(sig.removeprefix(_SIG_PREFIX))
    digest_bytes = bytes.fromhex(sha256_hex)
    for pub_pem in pubkey_ring:
        pub = serialization.load_pem_public_key(pub_pem.encode("utf-8"))
        if not isinstance(pub, ec.EllipticCurvePublicKey):
            continue
        try:
            pub.verify(der_sig, digest_bytes, ec.ECDSA(utils.Prehashed(hashes.SHA256())))
            return True
        except InvalidSignature:
            continue
    return False


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
# R5-spec-1 D4 / Patch A3: the end of a transaction the `cancel` verb stopped.
STATE_CANCELLED = "cancelled"

# R5-fw-2 / R5-spec-1 D11, retyped from `ff_mqtt.c` (`DETAIL_SELFTEST_*`): the simulator
# imports nothing from the server, so `tests/test_simulator.py` asserts that the server's
# parser (`selftest_outcome`) reads every detail composed from these.
# The simulator does NOT gate the confirm-timer or self-test rollback on a safe window
# (D12) and does NOT read `confirm_timeout_s` from the stage command: both are not
# modelled (a named gap, R5-test-1). Its `--safe-window` gates the apply and the three
# reboot-causing verbs only.
CAPABILITY_SELFTEST = "selftest"
SELFTEST_PASSED = "selftest passed"
SELFTEST_TIMED_OUT = "selftest timed out"
SELFTEST_FAILED = "selftest failed: "
RETURNED_TO = "; returned to "
# 27 bytes: with the prefix and `; returned to ota_N` the whole detail is 63 bytes, the
# firmware's `DETAIL_MAX`.
SELFTEST_FAIL_REASON = "simulated self-test failure"
SLOT_LABELS = ("ota_0", "ota_1")

# The R5 verbs (R5-spec-1 Patch A2; R5-fw-4/5 built them in `ff_mqtt.c`), retyped: each is
# both the command `type` and the capability a board announces when it implements it (D9).
# A board executes a verb only if it announces it; a board that announces none of them
# models a pre-R5 agent, which ignores a verb and sends no answer. `tests/test_simulator.py`
# asserts these agree with `broker/commands.py`.
COMMAND_APPLY = "apply"
COMMAND_CANCEL = "cancel"
COMMAND_ROLLBACK = "rollback"
COMMAND_REBOOT = "reboot"
VERB_COMMANDS = (COMMAND_APPLY, COMMAND_CANCEL, COMMAND_ROLLBACK, COMMAND_REBOOT)
# `up/ack.result` (Patch A5), retyped from `db/models.py::AckResult` (tripwire).
ACK_ACCEPTED = "accepted"
ACK_DEFERRED = "deferred"
ACK_REFUSED = "refused"
# The answers' `detail`, verbatim from `ff_mqtt.c` (*R5-fw-4 as built*, the vocabulary
# table): text for a server to show, never a contract. Each is at most 63 bytes.
ACK_NOTHING_STAGED = "nothing staged for this cmd_id"
ACK_NO_IMAGE = "no confirmed image to return to"
ACK_CANCEL_FIRST = "cancel first"
ACK_NOT_IMPLEMENTED = "not implemented"
ACK_NOTHING_TO_CANCEL = "nothing to cancel for this cmd_id"
ACK_UPDATE_IN_PROGRESS = "an update is in progress"
ACK_NOT_CONFIRMED = "the running image is not confirmed yet"
ACK_CMD_ID_MISSING = "cmd_id missing"
ACK_DETAILS = (
    ACK_NOTHING_STAGED,
    ACK_NO_IMAGE,
    ACK_CANCEL_FIRST,
    ACK_NOT_IMPLEMENTED,
    ACK_NOTHING_TO_CANCEL,
    ACK_UPDATE_IN_PROGRESS,
    ACK_NOT_CONFIRMED,
    ACK_CMD_ID_MISSING,
)
# R5-fw-5: the `awaiting_safe_window` details of a deferred verb.
DETAIL_APPLY_WAITS = "apply waits for a safe window"
DETAIL_ROLLBACK_WAITS = "rollback waits for a safe window"
# R5-fw-4: the head a commanded rollback stores; the returned-to image completes it with
# `; returned to <slot>`. Not a self-test outcome (the server's parser returns None).
ROLLED_BACK_BY_COMMAND = "rolled back by command"

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

# `spec/device-protocol.md` → *`dn/cmd` — commands*: `stage` (always executed) and the
# two `apply` modes. The R5 verbs above are executed only when announced.
COMMAND_STAGE = "stage"
APPLY_AUTO = "auto"
APPLY_ON_COMMAND = "on_command"

# `--safe-window`: whether this board ever judges the moment safe. `hold` parks in
# `awaiting_safe_window` forever, which is how "the server never expires it" becomes an
# observable property rather than a claim (`design/architecture.md` principle 5).
SAFE_WINDOW_AUTO = "auto"
SAFE_WINDOW_HOLD = "hold"
SAFE_WINDOW_MODES = (SAFE_WINDOW_AUTO, SAFE_WINDOW_HOLD)
# R5-test-1: `file:<path>` is a rule with both branches — the moment is safe iff <path>
# exists — so a harness opens the window by creating the file (no timer to race). Asked
# where the firmware asks its rule, and polled while something waits, every
# `SAFE_WINDOW_POLL_S`: the twin of `FF_SAFE_WINDOW_POLL_MS 250`
# (`ff_safe_window_internal.h`, a tripwire in `tests/test_simulator.py`).
SAFE_WINDOW_FILE_PREFIX = "file:"
SAFE_WINDOW_POLL_S = 0.25

# `--confirm`: what the image this board reboots into does. `auto` confirms at the
# announce PUBACK, like a healthy agent. `never` joins the fleet and never confirms — an
# `FF_ROLLBACK_TEST` image (`docs/runbooks/rollback-test.md`) — so the confirm timer
# fires and the board goes back to the version it came from. `--broken-marker TEXT` makes
# that per image instead of per board: an image whose version contains TEXT never confirms
# (an `FF_ROLLBACK_TEST` build is `<ver>-rbtest`), every other image follows `--confirm`.
CONFIRM_AUTO = "auto"
CONFIRM_NEVER = "never"
CONFIRM_MODES = (CONFIRM_AUTO, CONFIRM_NEVER)
# `spec/prd.md` → *Requirements & targets → Timing*: "confirm timeout 300 s". The real
# agent honours the command's `confirm_timeout_s` since R5-fw-3 (clamped 30-3600 s,
# counted from image start). This simulator keeps its own `--confirm-timeout`, which the
# tests use to shorten the wait; it does not read the command's (not modelled, a named gap).
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

# S0-fw-6, `spec/device-protocol.md` -> up/announce `reset_reason`: what a board reads
# after `esp_restart()`. The first start is the `DeviceIdentity` default, `poweron`.
RESET_REASON_SW = "sw"

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
    # Agent 0.4.6: the network joined and how many ff_cfg lists. Not checked by
    # `validate()`: the server refuses neither, and sending junk is how its tolerance
    # is tested.
    ssid: str | None = None
    known_networks: int | None = None
    power_class: str = "always_on"
    expected_wake_interval_s: int | None = None
    parent_device_id: str | None = None
    partition_layout: str = "ab-4m-v1"
    ota_slot_size: int = 1966080
    # The board measurements (R2b-be-6). Unset is null, as every agent <= 0.4.6 sends.
    # Typed loosely and not checked by `validate()`: the server refuses none of them,
    # and sending junk is how its tolerance is tested.
    flash_chip_size: int | None = None
    partition_table_sha256: str | None = None
    rollback_capable: bool | None = None
    capabilities: tuple[str, ...] = ()
    # S0-fw-6, `esp_reset_reason()` of the current boot, lowercased: `poweron` on the
    # first start, `sw` after the simulated OTA reboot (`StageRunner`). Not checked by
    # `validate()`: the server logs whatever arrives, and junk is how that is tested.
    reset_reason: str | None = "poweron"
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
            # Always present, null when unset, as agent 0.4.6 sends them.
            "ssid": self.ssid,
            "known_networks": self.known_networks,
            "power_class": self.power_class,
            "expected_wake_interval_s": self.expected_wake_interval_s,
            "parent_device_id": self.parent_device_id,
            "partition_layout": self.partition_layout,
            "ota_slot_size": self.ota_slot_size,
            # Always present, null when unset (R2b-be-6), in spec order.
            "flash_chip_size": self.flash_chip_size,
            "partition_table_sha256": self.partition_table_sha256,
            "rollback_capable": self.rollback_capable,
            "capabilities": list(self.capabilities),
            # S0-fw-6: after `capabilities`, as the spec prints it.
            "reset_reason": self.reset_reason,
        }

    def enroll_body(self, token: str) -> dict[str, object]:
        """`{ token, <the announce identity payload> }` — **flat, and it stays flat**.

        `spec/device-protocol.md` step 2 and `api/schemas.py::EnrollRequest` both say
        flat. A nested `{"token": …, "identity": {…}}` body is a recall, because the
        R0 agent is flash-baked and speaks whatever it was born with.
        """
        return {"token": token, **self.announce()}

    def heartbeat(self, uptime_s: int, *, boot_ok: bool = True) -> dict[str, object]:
        """`up/hb`, keys in the spec example's order.

        `rssi`, `free_heap` and `min_free_heap` are the spec example's constants (a
        simulated board has no radio and no heap; the low-water mark stays `<= free_heap`).
        `boot_ok` comes from the caller: `StageRunner.boot_ok`, false while the image this
        board rebooted into still waits to confirm (R4-fw-1).
        """
        return {
            "fw_version": self.fw_version,
            "uptime_s": uptime_s,
            "rssi": -61,
            "free_heap": 142000,
            "min_free_heap": 98000,
            "boot_ok": boot_ok,
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
    boot_ok: Callable[[], bool] = lambda: True,
) -> None:
    """One `up/hb` every `interval_s`, starting immediately. **Never retained.**

    `boot_ok` is called on every beat, never cached, as `ff_identity.c::boot_ok()` reads
    otadata on every heartbeat: a cached value would outlive the confirm.
    """
    while True:
        uptime_s = int(time.monotonic() - boot_monotonic)
        await _paced_publish(
            client,
            link,
            up_topic(device.device_id, HEARTBEAT),
            encode(device.heartbeat(uptime_s, boot_ok=boot_ok())),
            retain=False,
            step=step,
        )
        await asyncio.sleep(interval_s)


def _fetch(
    url: str,
    *,
    max_retries: int = 3,
    initial_backoff_s: float = 0.005,
    step: Step = _silent,
) -> bytes:
    """GET `url` into memory, chunked, with HTTP `Range` resume on interruption (`R6-FW-1`).
    **Blocking** — always called via `to_thread`.

    `urllib.request` and never httpx: this module ships in the production image and
    httpx is a dev dependency, which `tests/test_simulator.py`'s AST tripwire enforces.
    An ESP32 streams into the OTA partition instead of buffering; a 1.9 MB cap
    (`spec/prd.md` → *Capacity*) makes buffering fine here and keeps the sha256 check
    trivially correct.

    When a mid-stream read raises `OSError` after `offset > 0` bytes have arrived, the
    retry sends `Range: bytes=<offset>-` with exponential backoff (`initial_backoff_s * 2**n`)
    so the already-received prefix is never re-fetched. When `offset == 0`, `url` is passed
    as a plain `str` to `urlopen` so existing test monkeypatches keep seeing the bare URL.
    """
    buffer = bytearray()
    attempt = 0
    while True:
        offset = len(buffer)
        request_or_url: str | urllib.request.Request = (
            urllib.request.Request(url, headers={"Range": f"bytes={offset}-"})  # noqa: S310
            if offset > 0
            else url
        )
        try:
            with urllib.request.urlopen(  # noqa: S310
                request_or_url, timeout=DOWNLOAD_TIMEOUT_S
            ) as response:
                status_code = getattr(response, "status", None)
                if offset > 0 and status_code == 200:
                    # Upstream ignored `Range` and returned the full representation from byte 0.
                    buffer.clear()
                while True:
                    chunk = response.read(DOWNLOAD_CHUNK_BYTES)
                    if not chunk:
                        break
                    buffer.extend(chunk)
            return bytes(buffer)
        except OSError:
            attempt += 1
            if attempt > max_retries:
                raise
            delay_s = initial_backoff_s * (1 << (attempt - 1))
            step(
                f"download retry #{attempt}/{max_retries} from offset {len(buffer)} "
                f"after {delay_s:.3f}s"
            )
            if delay_s > 0:
                time.sleep(delay_s)


def check_safe_window(mode: str) -> None:
    """Refuse a `--safe-window` that is not `auto`, `hold` or `file:` + a non-empty PATH."""
    if mode in SAFE_WINDOW_MODES:
        return
    if mode.startswith(SAFE_WINDOW_FILE_PREFIX) and mode[len(SAFE_WINDOW_FILE_PREFIX) :]:
        return
    raise SimulatorError(
        f"--safe-window must be {SAFE_WINDOW_AUTO!r}, {SAFE_WINDOW_HOLD!r} or "
        f"'{SAFE_WINDOW_FILE_PREFIX}PATH' (safe while PATH exists), not {mode!r}"
    )


@dataclass(frozen=True, slots=True)
class PendingConfirm:
    """The NVS analogue: the transaction that crossed the apply reboot (`agent/components/fleetforge/src/ff_txn.h`).

    `previous_identity` is the board as it was before the apply — what a rollback returns
    it to, captured **before** `fw_version` was rebound.
    """

    cmd_id: str
    previous_identity: DeviceIdentity
    # The OTA slot the board ran from before the apply: what a self-test rollback names
    # in `; returned to <slot>` (R5-be-1).
    previous_slot: str = SLOT_LABELS[0]


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
    * `rolled_back_head` — the stored head of that `rolled_back` detail (`ff_txn.detail`):
      `selftest failed: <reason>` or `selftest timed out` when a registered self-test
      decided the rollback; None for the legacy wording. `on_boot` appends
      `; returned to <slot>`.
    * `slot` — the OTA slot label the running image is in. An apply flips it, a rollback
      flips it back; only the self-test details name it (R5-be-1).
    * `staged` — the `cmd_id` of a stage that stopped before its reboot: an
      `apply: "on_command"` one, or an `auto` one parked by an unsafe window. Its image
      is what the boot pointer now names, so while it is set a further `stage` is refused
      (R2-fw-2), as the firmware refuses to write a slot the boot pointer names. The
      `apply` verb clears it (and releases the image), the `cancel` verb clears it (and
      reports `cancelled`). `staged_version` is that image's version.
    * `apply_deferred` — the `cmd_id` whose release waits for a safe window; a repeat
      `apply` is an accepted no-op, a `cancel` still works (and clears it).
    * `rollback_deferred` / `reboot_deferred` — a reboot-causing verb answered `deferred`;
      a repeat is `accepted`.

    **The window** (`safe_window`): `auto` is always safe, `hold` never (so nothing it
    defers ever acts), and `file:<path>` is safe while <path> exists. `_unsafe_now()` is
    asked once per verb after every refusal (R5-fw-5 (3)), as the firmware asks its rule;
    only `file:` ever opens, and `release_if_safe` (polled by `_safe_window_loop`) then
    carries out what waited, as `ff_mqtt.c::command_reboot_task` does.
    * `returnable` — the confirmed image in the other slot, and that slot: what the
      `rollback` verb returns a confirmed board to. Set when a transaction confirms;
      cleared by any rollback. None on a fresh board, which models a USB flash whose other
      slot never held a confirmed image (`no confirmed image to return to`).

    **The verbs** (R5-spec-1 Patch A2) mirror `ff_mqtt.c` as *R5-fw-4/5 as built* describe
    it, and run only when the board announces them (D9). Each is answered once on `up/ack`
    (`_ack`), always before any status it causes. The simulator's download is inline, so
    a `cancel` can only ever meet a `staged` image.
    """

    identity: DeviceIdentity
    link: LinkProfile
    safe_window: str = SAFE_WINDOW_AUTO
    confirm: str = CONFIRM_AUTO
    confirm_timeout_s: float = DEFAULT_CONFIRM_TIMEOUT_S
    broken_marker: str | None = None
    # An image whose version carries this fails its registered self-test (R5-be-1).
    selftest_fail_marker: str | None = None
    # R6-SEC-1 / R6-SEC-2: require `artifact.sig` (`True`) or verify when present (`False`),
    # against the 2-key public key rotation ring.
    require_signature: bool = False
    signing_pubkey_ring: tuple[str, ...] = DEFAULT_SIGNING_PUBKEY_RING
    # R6-FW-1: HTTP Range resume retries and initial backoff on mid-stream download errors.
    download_retries: int = 3
    download_backoff_s: float = 0.005
    seen: set[str] = field(default_factory=set)
    downloads: int = 0
    # Not arguments: a caller passing in a pre-set event would start a board that reboots
    # before it has applied anything, and a pre-set transaction is one nobody issued.
    reboot: asyncio.Event = field(default_factory=asyncio.Event, init=False)
    pending: PendingConfirm | None = field(default=None, init=False)
    rolled_back: str | None = field(default=None, init=False)
    rolled_back_head: str | None = field(default=None, init=False)
    slot: str = field(default=SLOT_LABELS[0], init=False)
    staged: str | None = field(default=None, init=False)
    staged_version: str | None = field(default=None, init=False)
    apply_deferred: str | None = field(default=None, init=False)
    rollback_deferred: str | None = field(default=None, init=False)
    reboot_deferred: bool = field(default=False, init=False)
    returnable: tuple[DeviceIdentity, str] | None = field(default=None, init=False)
    # When the confirm timer of the current boot fires (monotonic). Armed once per boot,
    # like the firmware's esp_timer: a reconnect resumes it rather than restarting it.
    _confirm_deadline: float | None = field(default=None, init=False, repr=False)

    def __post_init__(self) -> None:
        check_safe_window(self.safe_window)
        if self.confirm not in CONFIRM_MODES:
            raise SimulatorError(
                f"--confirm must be one of {list(CONFIRM_MODES)}, not {self.confirm!r}"
            )
        if not self.confirm_timeout_s > 0:
            raise SimulatorError(
                f"--confirm-timeout must be a positive number of seconds, "
                f"not {self.confirm_timeout_s!r}"
            )
        if self.broken_marker == "":
            # `"" in version` is always true: every image would be the broken one.
            raise SimulatorError("--broken-marker must not be empty")
        if self.selftest_fail_marker == "":
            raise SimulatorError("--selftest-fail-marker must not be empty")
        if self.selftest_fail_marker is not None and not self._selftest_registered:
            raise SimulatorError(
                "an image can fail a self-test only if one is registered: "
                f"add --capabilities {CAPABILITY_SELFTEST}"
            )

    @property
    def boot_ok(self) -> bool:
        """`up/hb.boot_ok`: false only while the running image waits to confirm (R4-fw-1).

        The firmware's predicate is "otadata is not PENDING_VERIFY". Here that is a
        `pending` transaction this board has already rebooted into. An apply in the
        current session sets `pending` and `reboot` together: the image still running is
        the old, confirmed one, so it stays true until the session ends.
        """
        return self.pending is None or self.reboot.is_set()

    @property
    def _safe_window_path(self) -> str | None:
        """The PATH of a `file:PATH` window, or None (an empty PATH is no path)."""
        if self.safe_window.startswith(SAFE_WINDOW_FILE_PREFIX):
            return self.safe_window[len(SAFE_WINDOW_FILE_PREFIX) :] or None
        return None

    @property
    def window_can_open(self) -> bool:
        """Only a `file:` window ever opens after saying unsafe; `run_session` polls it."""
        return self._safe_window_path is not None

    def _unsafe_now(self) -> bool:
        """The board's rule, asked now: `hold` is never safe, `file:` is safe while the
        file exists, `auto` always is. Never cached: each verb asks it once."""
        if self.safe_window == SAFE_WINDOW_HOLD:
            return True
        path = self._safe_window_path
        if path is not None:
            return not Path(path).exists()
        return False

    def _waits(self, what: str) -> str:
        """The transcript line of a deferral."""
        if self.safe_window == SAFE_WINDOW_HOLD:
            return f"--safe-window hold: the {what} waits, indefinitely"
        return f"--safe-window {self.safe_window}: the {what} waits until the window opens"

    @property
    def _selftest_registered(self) -> bool:
        """A custom self-test is registered when the board announces the capability."""
        return CAPABILITY_SELFTEST in self.identity.capabilities

    @property
    def _answers_commands(self) -> bool:
        """An agent that implements any verb answers every id'd command on `up/ack`."""
        return any(verb in self.identity.capabilities for verb in VERB_COMMANDS)

    def _fails_selftest(self, version: str) -> bool:
        """An image whose version carries `selftest_fail_marker` fails its self-test."""
        return self.selftest_fail_marker is not None and self.selftest_fail_marker in version

    def _is_broken(self, version: str) -> bool:
        """An image whose version carries `broken_marker` never confirms (substring match)."""
        return self.broken_marker is not None and self.broken_marker in version

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

        if not isinstance(command_id, str) or not command_id:
            step(f"command  a {kind} with no id cannot be deduplicated — ignored")
            return
        if kind == COMMAND_STAGE:
            await self._stage(client, command_id, body, step)
            return
        if isinstance(kind, str) and kind in VERB_COMMANDS and kind in self.identity.capabilities:
            await self._verb(client, command_id, kind, body, step)
            return
        if not self._answers_commands:
            # A pre-R5 agent: it ignores the verb and sends nothing (D9).
            step(f"command  type={kind} is not implemented by this board's agent — ignored")
            return
        await self._ack(
            client,
            command_id,
            kind if isinstance(kind, str) else None,
            ACK_REFUSED,
            None,
            ACK_NOT_IMPLEMENTED,
            step,
        )

    async def _verb(
        self,
        client: aiomqtt.Client,
        command_id: str,
        kind: str,
        body: dict[str, object],
        step: Step,
    ) -> None:
        """Dispatch an announced R5 verb. `apply` / `cancel` need the `cmd_id` they name."""
        if kind in (COMMAND_APPLY, COMMAND_CANCEL):
            cmd_id = body.get("cmd_id")
            if not isinstance(cmd_id, str) or not cmd_id:
                await self._ack(
                    client, command_id, kind, ACK_REFUSED, None, ACK_CMD_ID_MISSING, step
                )
                return
            if kind == COMMAND_APPLY:
                await self._apply(client, command_id, cmd_id, step)
            else:
                await self._cancel(client, command_id, cmd_id, step)
        elif kind == COMMAND_ROLLBACK:
            await self._rollback_command(client, command_id, step)
        else:
            await self._reboot_command(client, command_id, step)

    async def _apply(
        self, client: aiomqtt.Client, command_id: str, cmd_id: str, step: Step
    ) -> None:
        """`ff_mqtt.c::on_apply`: release the staged image (D3), or say why not."""
        if self.staged == cmd_id:
            if self.apply_deferred == cmd_id:
                # A repeat while the release waits: changes nothing (R5-fw-5 (3)).
                await self._ack(client, command_id, COMMAND_APPLY, ACK_ACCEPTED, cmd_id, None, step)
                return
            if self._unsafe_now():
                await self._ack(client, command_id, COMMAND_APPLY, ACK_DEFERRED, cmd_id, None, step)
                await self._status(
                    client,
                    cmd_id,
                    STATE_AWAITING_SAFE_WINDOW,
                    step,
                    pct=100,
                    detail=DETAIL_APPLY_WAITS,
                )
                self.apply_deferred = cmd_id
                step(f"apply    {self._waits('release')}")
                return
            await self._ack(client, command_id, COMMAND_APPLY, ACK_ACCEPTED, cmd_id, None, step)
            version = self.staged_version
            self._clear_staged()
            await self._release(client, cmd_id, version, step)
            return
        if self.pending is not None and self.pending.cmd_id == cmd_id:
            # Already past `staged` (it rebooted into it): an accepted no-op (D3).
            await self._ack(client, command_id, COMMAND_APPLY, ACK_ACCEPTED, cmd_id, None, step)
            return
        await self._ack(
            client, command_id, COMMAND_APPLY, ACK_REFUSED, None, ACK_NOTHING_STAGED, step
        )

    async def _cancel(
        self, client: aiomqtt.Client, command_id: str, cmd_id: str, step: Step
    ) -> None:
        """`ff_mqtt.c::on_cancel` for a staged image (D4): put the boot pointer back, report
        `cancelled`. The download is inline here, so nothing else is ever cancellable."""
        if self.staged != cmd_id:
            await self._ack(
                client, command_id, COMMAND_CANCEL, ACK_REFUSED, None, ACK_NOTHING_TO_CANCEL, step
            )
            return
        await self._ack(client, command_id, COMMAND_CANCEL, ACK_ACCEPTED, cmd_id, None, step)
        await self._status(client, cmd_id, STATE_CANCELLED, step)
        self._clear_staged()
        step(f"cancel   {cmd_id} cancelled — the staged image will not boot")

    async def _rollback_command(self, client: aiomqtt.Client, command_id: str, step: Step) -> None:
        """`ff_mqtt.c::on_rollback` (D5): end an unconfirmed image's transaction, or roll a
        confirmed image back as a transaction of its own under the command's id."""
        if self.staged is not None:
            await self._ack(
                client,
                command_id,
                COMMAND_ROLLBACK,
                ACK_REFUSED,
                self.staged,
                ACK_CANCEL_FIRST,
                step,
            )
            return
        if self.rollback_deferred is not None:
            await self._ack(
                client,
                command_id,
                COMMAND_ROLLBACK,
                ACK_ACCEPTED,
                self.rollback_deferred,
                None,
                step,
            )
            return
        pending = self.pending
        if pending is not None and not self.reboot.is_set():
            # The image waits for its confirm (a `--confirm never` or broken image): the
            # rollback ends the stage's transaction, which the next session reports.
            if await self._deferred_rollback(client, command_id, pending.cmd_id, step):
                return
            await self._ack(
                client, command_id, COMMAND_ROLLBACK, ACK_ACCEPTED, pending.cmd_id, None, step
            )
            await self._carry_out_rollback(client, pending.cmd_id, step)
            return
        if self.returnable is None:
            await self._ack(
                client, command_id, COMMAND_ROLLBACK, ACK_REFUSED, None, ACK_NO_IMAGE, step
            )
            return
        # A confirmed image: its own transaction, under the command's id (D5). No
        # `requested` row exists for it on the server, so it stays out of the KPIs.
        if await self._deferred_rollback(client, command_id, command_id, step):
            return
        await self._ack(client, command_id, COMMAND_ROLLBACK, ACK_ACCEPTED, command_id, None, step)
        await self._carry_out_rollback(client, command_id, step)

    async def _carry_out_rollback(
        self, client: aiomqtt.Client, transaction: str, step: Step
    ) -> None:
        """The rollback an accepted verb, or a deferred one the window released, does.

        `transaction` is the unconfirmed image's `cmd_id` (the stage's transaction ends) or
        the command's own id (a confirmed image rolls back as a transaction of its own).
        """
        pending = self.pending
        if pending is not None and pending.cmd_id == transaction and not self.reboot.is_set():
            await self._rollback(client, pending, ROLLED_BACK_BY_COMMAND, step)
            return
        if self.returnable is None:
            # Only reachable through a release: the confirm timer rolled the image back
            # while the verb waited, so there is nothing left to return to.
            step(f"rollback {transaction}: nothing to return to any more — not carried out")
            return
        try:
            await self._status(client, transaction, STATE_ROLLING_BACK, step)
        except aiomqtt.MqttError as exc:
            step(f"rollback {STATE_ROLLING_BACK} not delivered ({exc}); rolling back anyway")
        identity, slot = self.returnable
        self.identity = replace(identity, reset_reason=RESET_REASON_SW)
        self.slot = slot
        self.rolled_back = transaction
        self.rolled_back_head = ROLLED_BACK_BY_COMMAND
        self.returnable = None
        self._confirm_deadline = None
        step(f"rollback returning to {identity.fw_version} in {slot}")
        self.reboot.set()

    async def _deferred_rollback(
        self, client: aiomqtt.Client, command_id: str, transaction: str, step: Step
    ) -> bool:
        """Under an unsafe window a rollback is answered `deferred` and waits (R5-fw-5)."""
        if not self._unsafe_now():
            return False
        await self._ack(client, command_id, COMMAND_ROLLBACK, ACK_DEFERRED, transaction, None, step)
        await self._status(
            client, transaction, STATE_AWAITING_SAFE_WINDOW, step, detail=DETAIL_ROLLBACK_WAITS
        )
        self.rollback_deferred = transaction
        step(f"rollback {self._waits('rollback')}")
        return True

    async def _reboot_command(self, client: aiomqtt.Client, command_id: str, step: Step) -> None:
        """`ff_mqtt.c::on_reboot` (D6): restart on the running image; no transaction, no
        status — the next announce's `reset_reason: sw` is the evidence."""
        if self.staged is not None:
            await self._ack(
                client,
                command_id,
                COMMAND_REBOOT,
                ACK_REFUSED,
                self.staged,
                ACK_UPDATE_IN_PROGRESS,
                step,
            )
            return
        if self.pending is not None and not self.reboot.is_set():
            await self._ack(
                client,
                command_id,
                COMMAND_REBOOT,
                ACK_REFUSED,
                self.pending.cmd_id,
                ACK_NOT_CONFIRMED,
                step,
            )
            return
        if self.reboot_deferred:
            await self._ack(client, command_id, COMMAND_REBOOT, ACK_ACCEPTED, None, None, step)
            return
        if self._unsafe_now():
            await self._ack(client, command_id, COMMAND_REBOOT, ACK_DEFERRED, None, None, step)
            self.reboot_deferred = True
            step(f"reboot   {self._waits('reboot')}")
            return
        await self._ack(client, command_id, COMMAND_REBOOT, ACK_ACCEPTED, None, None, step)
        self.identity = replace(self.identity, reset_reason=RESET_REASON_SW)
        step(f"reboot   restarting on {self.identity.fw_version} for command {command_id}")
        self.reboot.set()

    async def release_if_safe(self, client: aiomqtt.Client, step: Step) -> None:
        """Carry out what waits for the window, once the rule says safe.

        `ff_mqtt.c::command_reboot_task` / `ff_ota.c`'s wait for `apply: auto`: a released
        apply walks `applying → rebooting` exactly as an accepted one (D3), a released
        rollback does what an accepted one does, a released reboot restarts. A no-op while
        nothing waits, while the rule still says unsafe, or once a reboot is under way.
        `hold` never says safe, so under it nothing ever acts.
        """
        if self.reboot.is_set():
            return
        if self.apply_deferred is None and self.rollback_deferred is None:
            if not self.reboot_deferred:
                return
        if self._unsafe_now():
            return
        if self.apply_deferred is not None:
            cmd_id = self.apply_deferred
            version = self.staged_version
            # Cleared before the first publish: a `cancel` that arrives from here on finds
            # nothing staged, as on metal once the reboot task committed.
            self._clear_staged()
            step(f"window   the safe window opened: releasing the staged update {cmd_id}")
            await self._release(client, cmd_id, version, step)
            return
        if self.rollback_deferred is not None:
            transaction = self.rollback_deferred
            self.rollback_deferred = None
            step(f"window   the safe window opened: carrying out the rollback {transaction}")
            await self._carry_out_rollback(client, transaction, step)
            return
        self.reboot_deferred = False
        self.identity = replace(self.identity, reset_reason=RESET_REASON_SW)
        step(f"window   the safe window opened: restarting on {self.identity.fw_version}")
        self.reboot.set()

    def _clear_staged(self) -> None:
        self.staged = None
        self.staged_version = None
        self.apply_deferred = None

    async def _ack(
        self,
        client: aiomqtt.Client,
        command_id: str,
        kind: str | None,
        result: str,
        cmd_id: str | None,
        detail: str | None,
        step: Step,
    ) -> None:
        """One `up/ack`, QoS 1 and **never retained**, keys in Patch A5's order with
        explicit nulls (`ff_mqtt.c::enqueue_ack`)."""
        payload: dict[str, object] = {
            "id": command_id,
            "type": kind,
            "result": result,
            "cmd_id": cmd_id,
            "detail": detail,
        }
        await _paced_publish(
            client,
            self.link,
            up_topic(self.identity.device_id, ACK),
            encode(payload),
            retain=False,
            step=step,
        )
        step(f"ack      {kind} {result}" + (f" ({detail})" if detail else ""))

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
        sig = artifact.get("sig")
        if not isinstance(url, str) or not isinstance(expected_sha, str):
            await self._status(
                client, cmd_id, STATE_FAILED, step, detail="artifact url/sha256 missing"
            )
            return
        if not _SHA256_HEX.fullmatch(expected_sha):
            # Refused before any I/O, as the firmware does (R2-fw-1): no `staging`, no
            # download. A real board would otherwise erase its spare slot to find out.
            await self._status(
                client, cmd_id, STATE_FAILED, step, detail="artifact sha256 malformed"
            )
            return
        if sig is not None and not _is_valid_artifact_sig(sig):
            # R6-SEC-1: malformed `artifact.sig` is refused at the command seam (`ff_mqtt.c`),
            # before `staging` or any download.
            await self._status(client, cmd_id, STATE_FAILED, step, detail="artifact sig malformed")
            return

        await self._status(client, cmd_id, STATE_STAGING, step)

        # The firmware's two read-only refusals (ff_ota.c::choose_target_slot, R2-fw-2),
        # in its order and with its details: after `staging`, before the slot-size guard
        # and before any download.
        if self.pending is not None:
            step(f"stage    {self.pending.cmd_id} has not confirmed yet — refused, nothing fetched")
            await self._status(
                client, cmd_id, STATE_FAILED, step, detail="the running image is not confirmed yet"
            )
            return
        if self.staged is not None:
            step(
                f"stage    {self.staged} is staged and waits for a reboot — refused, nothing fetched"
            )
            await self._status(
                client,
                cmd_id,
                STATE_FAILED,
                step,
                detail="an update is already staged and waits for a reboot",
            )
            return

        size = artifact.get("size")
        if isinstance(size, int) and size > self.identity.ota_slot_size:
            # The firmware knows the slot only after `staging` (ff_ota.c::ota_task), and
            # refuses before anything is fetched or erased. Same order here.
            step(f"stage    {size} bytes will not fit a {self.identity.ota_slot_size}-byte slot")
            await self._status(
                client, cmd_id, STATE_FAILED, step, detail="artifact larger than the ota slot"
            )
            return

        await self._status(client, cmd_id, STATE_DOWNLOADING, step)
        try:
            data = await asyncio.to_thread(
                _fetch,
                url,
                max_retries=self.download_retries,
                initial_backoff_s=self.download_backoff_s,
                step=step,
            )
        except OSError as exc:
            # After exhausting `download_retries` Range resumes, report and stop.
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
        if isinstance(sig, str):
            if not _verify_artifact_sig(digest, sig, self.signing_pubkey_ring):
                step("verify   SIGNATURE INVALID against the trusted public key ring")
                await self._status(client, cmd_id, STATE_FAILED, step, detail="signature invalid")
                return
            step("verify   ecdsa-p256-sha256 signature verified")
        elif self.require_signature:
            step("verify   SIGNATURE MISSING (require_signature=True)")
            await self._status(client, cmd_id, STATE_FAILED, step, detail="signature missing")
            return

        await self._status(client, cmd_id, STATE_STAGED, step)

        staged_version = version if isinstance(version, str) and version else None
        if body.get("apply") == APPLY_ON_COMMAND:
            # `spec/device-protocol.md`: the device stages and waits for an explicit
            # `apply` (the verb, R5-be-2) — or a `cancel`. A further `stage` is refused
            # meanwhile (`staged`).
            self.staged = cmd_id
            self.staged_version = staged_version
            step("stage    apply=on_command — staying staged until an apply arrives")
            return

        if self._unsafe_now():
            # Parked. Nothing server-side may end this: a vehicle in motion reports
            # honestly for as long as it is moving (under `hold`, permanently and on
            # purpose; a `file:` window releases it when it opens). The image is staged
            # all the same (a `cancel` can still end it, R5-fw-5 (5)).
            await self._status(client, cmd_id, STATE_AWAITING_SAFE_WINDOW, step)
            self.staged = cmd_id
            self.staged_version = staged_version
            self.apply_deferred = cmd_id
            if self.safe_window == SAFE_WINDOW_HOLD:
                step("stage    --safe-window hold: parked in awaiting_safe_window, indefinitely")
            else:
                step(f"stage    {self._waits('apply')}: parked in awaiting_safe_window")
            return

        await self._release(client, cmd_id, staged_version, step)

    async def _release(
        self, client: aiomqtt.Client, cmd_id: str, version: str | None, step: Step
    ) -> None:
        """`applying → rebooting`, then the reboot: one walk for `apply: auto` and for the
        `apply` verb, so a commanded release is exactly an automatic one (D3)."""
        await self._status(client, cmd_id, STATE_APPLYING, step)
        await self._status(client, cmd_id, STATE_REBOOTING, step)
        # The transaction crosses the reboot (the firmware writes it to NVS at `staged`;
        # nothing here can reset between the two, so recording it at the reboot is the
        # same). `previous_identity` is taken BEFORE the rebind below: it is what a
        # rollback returns the board to.
        self.pending = PendingConfirm(
            cmd_id=cmd_id, previous_identity=self.identity, previous_slot=self.slot
        )
        self.slot = SLOT_LABELS[1] if self.slot == SLOT_LABELS[0] else SLOT_LABELS[0]
        self._confirm_deadline = None
        # The apply reboot is `esp_restart()`, so the next boot reads ESP_RST_SW (S0-fw-6).
        self.identity = replace(self.identity, reset_reason=RESET_REASON_SW)
        if version:
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
            head = self.rolled_back_head
            await self._status(
                client,
                self.rolled_back,
                STATE_ROLLED_BACK,
                step,
                detail=(
                    f"{head}{RETURNED_TO}{self.slot}"
                    if head is not None
                    else f"returned to {self.identity.fw_version}; the new image did not confirm"
                ),
            )
            self.rolled_back = None
            self.rolled_back_head = None
            return None

        pending = self.pending
        if pending is None:
            return None
        await self._status(client, pending.cmd_id, STATE_CONFIRMING, step)
        broken = self._is_broken(self.identity.fw_version)
        registered = self._selftest_registered
        if self.confirm == CONFIRM_AUTO and not broken:
            if registered and self._fails_selftest(self.identity.fw_version):
                # R5-fw-2: the self-test failed, so the image never confirms and the
                # agent rolls back at once. `rolling_back` is best-effort; the reboot
                # that follows ends this session before any heartbeat.
                step(
                    f"selftest {self.identity.fw_version} failed its self-test "
                    f"({SELFTEST_FAIL_REASON}) — rolling back now"
                )
                await self._rollback(client, pending, SELFTEST_FAILED + SELFTEST_FAIL_REASON, step)
                return None
            await self._status(
                client,
                pending.cmd_id,
                STATE_CONFIRMED,
                step,
                pct=100,
                detail=SELFTEST_PASSED if registered else None,
            )
            step(f"confirm  {self.identity.fw_version} confirmed at the announce ack")
            # The image it came from was confirmed in the other slot: a `rollback` verb
            # may now return to it (R5-fw-4 `other_slot_is_returnable`).
            self.returnable = (pending.previous_identity, pending.previous_slot)
            self.pending = None
            self._confirm_deadline = None
            return None

        if self._confirm_deadline is None:
            self._confirm_deadline = time.monotonic() + self.confirm_timeout_s
        remaining = max(0.0, self._confirm_deadline - time.monotonic())
        why = (
            f"{self.identity.fw_version} carries the broken marker {self.broken_marker!r}"
            if broken
            else "--confirm never"
        )
        step(f"confirm  {why}: rolling back in {remaining:.1f}s unless confirmed")
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
        # The firmware pre-stores `selftest timed out` at the ack, so a registered
        # self-test that never finished reports that; otherwise the legacy wording.
        await self._rollback(
            client, pending, SELFTEST_TIMED_OUT if self._selftest_registered else None, step
        )

    async def _rollback(
        self, client: aiomqtt.Client, pending: PendingConfirm, head: str | None, step: Step
    ) -> None:
        """The one rollback the confirm timer, a failed self-test and a `rollback` verb
        during the confirm end in.

        `rolling_back` is attempted BEFORE the reboot is triggered (see `_confirm_timeout`);
        `head` is the stored detail head the next session completes with the slot label.
        The slot it leaves is marked invalid, so nothing is returnable afterwards.
        """
        try:
            await self._status(client, pending.cmd_id, STATE_ROLLING_BACK, step)
        except aiomqtt.MqttError as exc:
            step(f"confirm  {STATE_ROLLING_BACK} not delivered ({exc}); rolling back anyway")
        # The rollback ends in `esp_restart()` too: `sw` (S0-fw-6).
        self.identity = replace(pending.previous_identity, reset_reason=RESET_REASON_SW)
        self.slot = pending.previous_slot
        self.rolled_back = pending.cmd_id
        self.rolled_back_head = head
        self.pending = None
        self.returnable = None
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


async def _telemetry_loop(
    client: aiomqtt.Client, device: DeviceIdentity, buffer: TelemetryBuffer, step: Step
) -> None:
    """`ff_telemetry.c::flush_task`: drain on connect, then at most one payload per 5 s.

    QoS 0, **never retained**. A sample is committed (forgotten) only after its publish
    went out, so a session that dies here leaves it for the next connect.
    """
    topic = up_topic(device.device_id, TELEMETRY)

    async def publish_one() -> bool:
        built = buffer.next_payload()
        if built is None:
            return False
        payload, last_seq, dropped = built
        await client.publish(topic, payload, qos=TELEMETRY_QOS, retain=False)
        buffer.commit(last_seq, dropped)
        samples = payload.count(b'"age_ms":')
        step(
            f"publish  {topic} (qos {TELEMETRY_QOS}, no retain, "
            f"{samples} samples, dropped {dropped}, {len(payload)} B)"
        )
        return True

    while await publish_one():
        pass
    while True:
        await asyncio.sleep(TELEMETRY_FLUSH_S)
        await publish_one()


async def _sampler_loop(buffer: TelemetryBuffer, every_s: float, stop: asyncio.Event) -> None:
    """The board's own firmware: `sim_counter` = 1, 2, 3, ... every `every_s`, connected or
    not, on a fixed schedule (no drift), for the whole life of the process."""
    start = time.monotonic()
    counter = 0
    while not stop.is_set():
        counter += 1
        buffer.record("sim_counter", counter)
        await _sleep_until(stop, max(start + counter * every_s - time.monotonic(), 0.0))


def _start_sampler(
    every_s: float | None, stop: asyncio.Event
) -> tuple[TelemetryBuffer | None, asyncio.Task[None] | None]:
    """The buffer and its sampler, built OUTSIDE the reconnect/wake loop (like
    `StageRunner`), so samples taken while disconnected survive to the next session."""
    if every_s is None:
        return None, None
    buffer = TelemetryBuffer()
    return buffer, asyncio.create_task(_sampler_loop(buffer, every_s, stop))


async def _stop_sampler(task: asyncio.Task[None] | None) -> None:
    if task is not None:
        task.cancel()
        with contextlib.suppress(asyncio.CancelledError):
            await task


async def _safe_window_loop(client: aiomqtt.Client, stage: StageRunner, step: Step) -> None:
    """Ask a `file:` window every `SAFE_WINDOW_POLL_S` and release what waits on it.

    A worker, so it never returns normally (a returning worker means "the broker closed
    the stream"); the state lives on `StageRunner`, so a reconnect resumes the wait.
    """
    while True:
        await stage.release_if_safe(client, step)
        await asyncio.sleep(SAFE_WINDOW_POLL_S)


async def _command_loop(client: aiomqtt.Client, stage: StageRunner, step: Step) -> None:
    """Drain `dn/#` and hand every message to the stage runner.

    R0 logged commands and executed nothing; since R1-be-2 a `stage` is really carried
    out — downloaded, verified against its sha256 and "applied" — so the acceptance
    criterion ("the device fetched the bytes") is observable rather than asserted.
    Since R5-be-2 the four verbs (`apply`, `cancel`, `rollback`, `reboot`) are carried
    out and answered on `up/ack` by a board that announces them; such a board answers
    `identify` and `set_cfg` `refused` / `not implemented`, and a board that announces
    none of them ignores every non-stage command, as a pre-R5 agent does.
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
    telemetry: TelemetryBuffer | None = None,
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
    5. heartbeats and the command reader, concurrently, plus the `up/telemetry` drain
       when `telemetry` is given (R4-fw-2: after the outcome, as in `ff_mqtt.c`);
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
                _heartbeat_loop(
                    client,
                    device,
                    link,
                    heartbeat_interval_s,
                    boot_monotonic,
                    step,
                    boot_ok=lambda: stage.boot_ok,
                )
            ),
            asyncio.create_task(_command_loop(client, stage, step)),
        ]
        if telemetry is not None:
            workers.append(asyncio.create_task(_telemetry_loop(client, device, telemetry, step)))
        if stage.window_can_open:
            workers.append(asyncio.create_task(_safe_window_loop(client, stage, step)))
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
    broken_marker: str | None = None,
    selftest_fail_marker: str | None = None,
    telemetry_every_s: float | None = None,
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

    `telemetry_every_s` (R4-fw-2) records `sim_counter` on that period for the board's
    whole life; samples taken while the broker is away are sent after the reconnect.
    """
    telemetry, sampler = _start_sampler(telemetry_every_s, stop)
    try:
        await _always_on(
            device,
            credential,
            client_factory=client_factory,
            link=link,
            heartbeat_interval_s=heartbeat_interval_s,
            stop=stop,
            endpoint=endpoint,
            safe_window=safe_window,
            confirm=confirm,
            confirm_timeout_s=confirm_timeout_s,
            broken_marker=broken_marker,
            selftest_fail_marker=selftest_fail_marker,
            telemetry=telemetry,
            step=step,
        )
    finally:
        await _stop_sampler(sampler)


async def _always_on(
    device: DeviceIdentity,
    credential: Credential,
    *,
    client_factory: ClientFactory,
    link: LinkProfile,
    heartbeat_interval_s: float,
    stop: asyncio.Event,
    endpoint: str,
    safe_window: str,
    confirm: str,
    confirm_timeout_s: float,
    broken_marker: str | None,
    selftest_fail_marker: str | None,
    telemetry: TelemetryBuffer | None,
    step: Step,
) -> None:
    """`run_always_on`'s loop, given the board-lifetime telemetry buffer."""
    boot_monotonic = time.monotonic()
    connected = asyncio.Event()
    stage = StageRunner(
        identity=device,
        link=link,
        safe_window=safe_window,
        confirm=confirm,
        confirm_timeout_s=confirm_timeout_s,
        broken_marker=broken_marker,
        selftest_fail_marker=selftest_fail_marker,
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
                telemetry=telemetry,
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
                if telemetry is not None:
                    telemetry.clear()  # the ring is RAM
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
    broken_marker: str | None = None,
    selftest_fail_marker: str | None = None,
    telemetry_every_s: float | None = None,
    step: Step = _silent,
) -> None:
    """wake → connect → announce/presence/hb → drain `dn/` → disconnect → sleep → repeat.

    **No goodbye between wakes.** For a sleepy board `presence.is_online` ignores
    `presence_reported` entirely and uses `last_seen`, so a `{"online":false}` would be
    noise the server is contractually required to ignore — and the ingestor would drop
    it anyway. This is the only mode that exercises the
    `2.5 x expected_wake_interval_s` branch end to end.

    `telemetry_every_s` (R4-fw-2) keeps sampling through the sleep, so each wake drains
    what the board recorded while it was away.
    """
    telemetry, sampler = _start_sampler(telemetry_every_s, stop)
    try:
        await _sleepy(
            device,
            credential,
            client_factory=client_factory,
            link=link,
            heartbeat_interval_s=heartbeat_interval_s,
            wake_interval_s=wake_interval_s,
            awake_s=awake_s,
            stop=stop,
            endpoint=endpoint,
            safe_window=safe_window,
            confirm=confirm,
            confirm_timeout_s=confirm_timeout_s,
            broken_marker=broken_marker,
            selftest_fail_marker=selftest_fail_marker,
            telemetry=telemetry,
            step=step,
        )
    finally:
        await _stop_sampler(sampler)


async def _sleepy(
    device: DeviceIdentity,
    credential: Credential,
    *,
    client_factory: ClientFactory,
    link: LinkProfile,
    heartbeat_interval_s: float,
    wake_interval_s: float,
    awake_s: float,
    stop: asyncio.Event,
    endpoint: str,
    safe_window: str,
    confirm: str,
    confirm_timeout_s: float,
    broken_marker: str | None,
    selftest_fail_marker: str | None,
    telemetry: TelemetryBuffer | None,
    step: Step,
) -> None:
    """`run_sleepy`'s loop, given the board-lifetime telemetry buffer."""
    boot_monotonic = time.monotonic()
    stage = StageRunner(
        identity=device,
        link=link,
        safe_window=safe_window,
        confirm=confirm,
        confirm_timeout_s=confirm_timeout_s,
        broken_marker=broken_marker,
        selftest_fail_marker=selftest_fail_marker,
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
            telemetry=telemetry,
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
            if telemetry is not None:
                telemetry.clear()  # the ring is RAM
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

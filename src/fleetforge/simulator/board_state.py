"""Protocol constants, wire encoding, signature verification, and board identity models.

Kept strictly server-independent (`tests/test_simulator.py` AST purity guard): imports
nothing from `fleetforge` outside `fleetforge.identity` and `fleetforge.simulator.*`.
"""

from __future__ import annotations

import asyncio
import base64
import binascii
import hashlib
import json
import os
import random
import re
import urllib.parse
from collections.abc import Callable
from dataclasses import dataclass, field

import aiomqtt
from cryptography.exceptions import InvalidSignature
from cryptography.hazmat.primitives import hashes, serialization
from cryptography.hazmat.primitives.asymmetric import ec, utils

from fleetforge.identity import DEVICE_ID_RE
from fleetforge.simulator.errors import SimulatorError

TOPIC_ROOT = "ff/v1/d"

ANNOUNCE = "announce"
PRESENCE = "presence"
HEARTBEAT = "hb"
STATUS = "status"
TELEMETRY = "telemetry"
COMMAND = "cmd"
ACK = "ack"

_SHA256_HEX = re.compile(r"[0-9a-f]{64}")

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


def _decode_env_pem_text(raw: str) -> str:
    text = raw.strip().replace("\\n", "\n")
    if text.startswith("-----BEGIN "):
        return text
    try:
        decoded = base64.b64decode(text, validate=True).decode("utf-8").strip()
    except (binascii.Error, UnicodeDecodeError):
        return text
    return decoded if decoded.startswith("-----BEGIN ") else text


def _canonical_p256_pub_pem(raw_item: str) -> str:
    candidate = _decode_env_pem_text(raw_item)
    pub = serialization.load_pem_public_key((candidate + "\n").encode("utf-8"))
    if not isinstance(pub, ec.EllipticCurvePublicKey):
        raise SimulatorError("ARTIFACT_SIGNING_PUBLIC_KEYS must contain P-256 public keys")
    return pub.public_bytes(
        serialization.Encoding.PEM,
        serialization.PublicFormat.SubjectPublicKeyInfo,
    ).decode("utf-8")


def resolve_signing_pubkey_ring(raw: str | None = None) -> tuple[str, ...]:
    """Resolve trusted P-256 public keys from `raw` or `$ARTIFACT_SIGNING_PUBLIC_KEYS`."""
    env_val = raw if raw is not None else os.environ.get("ARTIFACT_SIGNING_PUBLIC_KEYS")
    if not env_val or not env_val.strip():
        return DEFAULT_SIGNING_PUBKEY_RING
    text = _decode_env_pem_text(env_val)
    blocks = re.findall(r"-----BEGIN PUBLIC KEY-----.*?-----END PUBLIC KEY-----", text, re.DOTALL)
    candidates = blocks if blocks else [p.strip() for p in text.split(";") if p.strip()]
    parsed = list(dict.fromkeys(_canonical_p256_pub_pem(item) for item in candidates))
    return (*parsed, *DEFAULT_SIGNING_PUBKEY_RING) if parsed else DEFAULT_SIGNING_PUBKEY_RING


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


STATE_STAGING = "staging"
STATE_DOWNLOADING = "downloading"
STATE_VERIFYING = "verifying"
STATE_STAGED = "staged"
STATE_AWAITING_SAFE_WINDOW = "awaiting_safe_window"
STATE_APPLYING = "applying"
STATE_REBOOTING = "rebooting"
STATE_FAILED = "failed"
STATE_CONFIRMING = "confirming"
STATE_CONFIRMED = "confirmed"
STATE_ROLLING_BACK = "rolling_back"
STATE_ROLLED_BACK = "rolled_back"
STATE_CANCELLED = "cancelled"

CAPABILITY_SELFTEST = "selftest"
SELFTEST_PASSED = "selftest passed"
SELFTEST_TIMED_OUT = "selftest timed out"
SELFTEST_FAILED = "selftest failed: "
RETURNED_TO = "; returned to "
SELFTEST_FAIL_REASON = "simulated self-test failure"
SLOT_LABELS = ("ota_0", "ota_1")

COMMAND_APPLY = "apply"
COMMAND_CANCEL = "cancel"
COMMAND_ROLLBACK = "rollback"
COMMAND_REBOOT = "reboot"
VERB_COMMANDS = (COMMAND_APPLY, COMMAND_CANCEL, COMMAND_ROLLBACK, COMMAND_REBOOT)

BASE_AGENT_CAPABILITIES: tuple[str, ...] = (
    "ota",
    COMMAND_APPLY,
    COMMAND_CANCEL,
    COMMAND_ROLLBACK,
    COMMAND_REBOOT,
)
OPTIONAL_AGENT_CAPABILITIES: tuple[str, ...] = (
    CAPABILITY_SELFTEST,
    "safe_window",
    "sig",
)
DEFAULT_AGENT_CAPABILITIES: tuple[str, ...] = (
    *BASE_AGENT_CAPABILITIES,
    "sig",
)
ALL_BUILTIN_CAPABILITIES: tuple[str, ...] = (
    *BASE_AGENT_CAPABILITIES,
    *OPTIONAL_AGENT_CAPABILITIES,
)

ACK_ACCEPTED = "accepted"
ACK_DEFERRED = "deferred"
ACK_REFUSED = "refused"

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
DETAIL_APPLY_WAITS = "apply waits for a safe window"
DETAIL_ROLLBACK_WAITS = "rollback waits for a safe window"
ROLLED_BACK_BY_COMMAND = "rolled back by command"

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

COMMAND_STAGE = "stage"
APPLY_AUTO = "auto"
APPLY_ON_COMMAND = "on_command"

SAFE_WINDOW_AUTO = "auto"
SAFE_WINDOW_HOLD = "hold"
SAFE_WINDOW_MODES = (SAFE_WINDOW_AUTO, SAFE_WINDOW_HOLD)
SAFE_WINDOW_FILE_PREFIX = "file:"
SAFE_WINDOW_POLL_S = 0.25

CONFIRM_AUTO = "auto"
CONFIRM_NEVER = "never"
CONFIRM_MODES = (CONFIRM_AUTO, CONFIRM_NEVER)
DEFAULT_CONFIRM_TIMEOUT_S = 300.0

DOWNLOAD_CHUNK_BYTES = 64 * 1024
DOWNLOAD_TIMEOUT_S = 30.0

PRESENCE_ONLINE = b'{"online":true}'
PRESENCE_OFFLINE = b'{"online":false}'

QOS = 1
KEEPALIVE_S = 30
DEFAULT_HEARTBEAT_INTERVAL_S = 60.0
DEFAULT_AWAKE_S = 3.0

SLOW_MIN_DELAY_S = 0.5
SLOW_MAX_DELAY_S = 3.0
SLOW_ENROLL_EXTRA_S = 2.0

LINK_FAST = "fast"
LINK_SLOW = "slow"
LINK_PROFILES = (LINK_FAST, LINK_SLOW)

POWER_CLASSES = ("always_on", "sleepy")
RESET_REASON_SW = "sw"

ClientFactory = Callable[[], aiomqtt.Client]
Step = Callable[[str], None]


def _silent(_message: str) -> None:
    """The default transcript sink: a library call prints nothing."""


def derive_device_id(name: str) -> str:
    """A stable, locally-administered unicast pseudo-MAC for a simulated board named `name`."""
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
    """`https://host/…` — scheme and host only; signed query strings are bearer credentials."""
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
    """The Last Will: retained `{"online":false}` on `up/presence`, QoS 1."""
    return aiomqtt.Will(
        topic=up_topic(device_id, PRESENCE),
        payload=PRESENCE_OFFLINE,
        qos=QOS,
        retain=True,
    )


@dataclass(frozen=True, slots=True)
class DeviceIdentity:
    """Exactly the `up/announce` identity, and therefore exactly the enroll body."""

    device_id: str
    platform_type: str = "esp32c6"
    fw_version: str = "1.4.2"
    agent_version: str = "0.1.0-sim"
    link_type: str = "wifi"
    ssid: str | None = None
    known_networks: int | None = None
    power_class: str = "always_on"
    expected_wake_interval_s: int | None = None
    parent_device_id: str | None = None
    partition_layout: str = "ab-4m-v1"
    ota_slot_size: int = 1966080
    flash_chip_size: int | None = None
    partition_table_sha256: str | None = None
    rollback_capable: bool | None = None
    capabilities: tuple[str, ...] = ()
    reset_reason: str | None = "poweron"
    sub_versions: dict[str, str] | None = None
    proto: int = 1

    def validate(self) -> None:
        """Refuse an identity the server would refuse — before any I/O."""
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
        """The `up/announce` body, key for key as `spec/device-protocol.md` prints it."""
        return {
            "proto": self.proto,
            "device_id": self.device_id,
            "platform_type": self.platform_type,
            "fw_version": self.fw_version,
            "agent_version": self.agent_version,
            "link_type": self.link_type,
            "ssid": self.ssid,
            "known_networks": self.known_networks,
            "power_class": self.power_class,
            "expected_wake_interval_s": self.expected_wake_interval_s,
            "parent_device_id": self.parent_device_id,
            "partition_layout": self.partition_layout,
            "ota_slot_size": self.ota_slot_size,
            "flash_chip_size": self.flash_chip_size,
            "partition_table_sha256": self.partition_table_sha256,
            "rollback_capable": self.rollback_capable,
            "capabilities": list(self.capabilities),
            "reset_reason": self.reset_reason,
            **({"sub_versions": dict(self.sub_versions)} if self.sub_versions else {}),
        }

    def enroll_body(self, token: str) -> dict[str, object]:
        """`{ token, <the announce identity payload> }` — flat, as `EnrollRequest` expects."""
        return {"token": token, **self.announce()}

    def heartbeat(self, uptime_s: int, *, boot_ok: bool = True) -> dict[str, object]:
        """`up/hb`, keys in the spec example's order."""
        return {
            "fw_version": self.fw_version,
            "uptime_s": uptime_s,
            "rssi": -61,
            "free_heap": 142000,
            "min_free_heap": 98000,
            "boot_ok": boot_ok,
        }


def encode(payload: dict[str, object]) -> bytes:
    """Compact UTF-8 JSON."""
    return json.dumps(payload, separators=(",", ":")).encode("utf-8")


@dataclass(slots=True)
class LinkProfile:
    """A deterministic bad link: seeded latency before every publish."""

    name: str = LINK_FAST
    seed: int = 0
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
        return self._random.uniform(SLOW_MIN_DELAY_S, SLOW_MAX_DELAY_S)  # noqa: S311

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
    """Publish one QoS-1 message, after the link profile's delay."""
    delay = link.next_delay()
    if delay:
        step(f"link     +{delay:.2f}s of latency before {topic}")
        await asyncio.sleep(delay)
    await client.publish(topic, payload, qos=QOS, retain=retain)
    step(f"publish  {topic} (qos {QOS}, {'retain' if retain else 'no retain'})")


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
    """The NVS analogue: the transaction that crossed the apply reboot."""

    cmd_id: str
    previous_identity: DeviceIdentity
    previous_slot: str = SLOT_LABELS[0]

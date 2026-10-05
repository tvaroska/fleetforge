"""Device-reported fields that both edges ingest: the announce and the enroll body.

`spec/device-protocol.md` → `up/announce`: agent 0.4.6 carries `"ssid"` and
`"known_networks"` right after `link_type`, both `null` on ethernet, both absent on
older agents. "The server treats absent and `null` alike, as 'not reported'", and "the
enroll body carries the same fields, and the server stores a malformed value as null
rather than refusing the request".

**Store, never reject.** A normaliser here never raises. On the MQTT side a
`ValidationError` makes `decode()` drop the **whole** announce — including the
`fw_version` that proves an OTA landed — and because the announce is retained, the
same drop repeats on every reconnect. On the HTTP side the spec forbids refusing the
request. So a value we cannot use becomes `None`, and the rest of the message stands.

Two gotchas decide the `ssid` rules. A NUL (`\\x00`) in a TEXT value makes PostgreSQL
raise inside the transaction, which rolls back the announce it rode in on. And JSON
can carry a lone surrogate (`"\\ud800"`), which `json.loads` accepts but which cannot
be encoded as UTF-8, so asyncpg would raise on it too. Both become `None` here, before
anything reaches the database.

**Never log the value.** An SSID is the operator's data and logs are not a data store
(the same rule `ingestor/protocol.py::decode` follows by logging only a length). A
malformed value logs its field name, a reason and the device id, at INFO: one line per
board session, so a buggy fleet does not flood.

**The board measurements (R2b-be-6).** `flash_chip_size`, `partition_table_sha256` and
`rollback_capable` follow right after `ota_slot_size`; an absent field "means unknown,
and the server never refuses a device for omitting one". Malformed means: a
`flash_chip_size` that is not a positive integer, a `partition_table_sha256` that is not
64 lowercase hex characters, a `rollback_capable` that is not a boolean
(`docs/features/board-profiles.md` → *Step 1 wire proposal*). Nothing is coerced: not
`"4194304"` or `4194304.0` to an int, not an uppercase digest to lowercase (uppercase is
malformed by definition), not `"true"` or `1` to a bool. `flash_chip_size` also has a
ceiling, the `uint32_t` the agent reads it into. It is load-bearing: an integer past
int64 (`2**63`, or a JSON literal like `1e30` written out in full) makes asyncpg raise
inside the transaction, and the announce it rode in on is lost.

This lives outside both `fleetforge.api` and `fleetforge.ingestor` because the API may
not import from the ingestor (the precedent is `identity.py`). It is named for
announced fields in general.
"""

import logging
import re
import unicodedata

logger = logging.getLogger(__name__)

# The 802.11 SSID limit, in bytes. Matches `agent/tools/ff_cfg.py::MAX_SSID_BYTES` and
# `ff_cfg.h`.
SSID_MAX_BYTES = 32

# A plausibility ceiling, not the protocol cap (4 today). A later writer may raise the
# cap, and its boards must still be stored.
KNOWN_NETWORKS_MAX = 64

# The `uint32_t` that `esp_flash_get_physical_size` writes. Also keeps every stored value
# inside the BIGINT column: past int64, asyncpg raises and the announce is lost.
FLASH_CHIP_SIZE_MAX = 0xFFFF_FFFF

# Exactly 64 lowercase hex characters, used with `fullmatch`. An explicit class on
# purpose: `\d` would accept Unicode digits such as "٠", and `re.match(...$)` would
# accept a trailing newline. The class also keeps out NUL and lone surrogates, which
# PostgreSQL TEXT and asyncpg would refuse.
_SHA256_HEX = re.compile(r"[0-9a-f]{64}")


def _unusable(field: str, reason: str, device_id: str | None) -> None:
    """Log one malformed field: the name and why, never the value."""
    logger.info(
        "device %s announced an unusable %s (%s); stored as null",
        device_id or "?",
        field,
        reason,
    )


def normalize_ssid(value: object, *, device_id: str | None = None) -> str | None:
    """The SSID exactly as the board reported it, or `None` if it is not usable.

    No stripping and no case change: the SSID is stored "exactly as written in ff_cfg".
    Empty is "not reported", not a malformed value, so it logs nothing.
    """
    if value is None:
        return None
    if not isinstance(value, str):
        _unusable("ssid", "not a string", device_id)
        return None
    if value == "":
        return None
    try:
        size = len(value.encode("utf-8"))
    except UnicodeEncodeError:
        _unusable("ssid", "not valid unicode", device_id)
        return None
    if size > SSID_MAX_BYTES:
        _unusable("ssid", f"{size} bytes > {SSID_MAX_BYTES}", device_id)
        return None
    if any(unicodedata.category(ch) == "Cc" for ch in value):
        _unusable("ssid", "control character", device_id)
        return None
    return value


def normalize_known_networks(value: object, *, device_id: str | None = None) -> int | None:
    """How many networks the board knows, or `None` if that is not a plausible count.

    `bool` is an `int` in Python and is not a count here. Floats (`2.0`) and strings
    (`"2"`) are not coerced: the agent writes a JSON integer, and anything else is a
    broken writer whose number we have no reason to trust.
    """
    if value is None:
        return None
    if isinstance(value, bool) or not isinstance(value, int):
        _unusable("known_networks", "not an integer", device_id)
        return None
    if not 0 <= value <= KNOWN_NETWORKS_MAX:
        _unusable("known_networks", "out of range", device_id)
        return None
    return value


def normalize_flash_chip_size(value: object, *, device_id: str | None = None) -> int | None:
    """The physical flash chip size in bytes, or `None` if it is not a positive integer.

    `bool` is an `int` in Python and is not a size. Floats and strings are not coerced.
    No power-of-two rule: the spec says only "positive integer".
    """
    if value is None:
        return None
    if isinstance(value, bool) or not isinstance(value, int):
        _unusable("flash_chip_size", "not an integer", device_id)
        return None
    if not 1 <= value <= FLASH_CHIP_SIZE_MAX:
        _unusable("flash_chip_size", "out of range", device_id)
        return None
    return value


def normalize_partition_table_sha256(value: object, *, device_id: str | None = None) -> str | None:
    """The partition table fingerprint, or `None` if it is not 64 lowercase hex characters.

    Never lowercased: the spec defines the value as lowercase hex, so an uppercase digest
    comes from a broken writer. `""` is malformed too, not "not reported".
    """
    if value is None:
        return None
    if not isinstance(value, str):
        _unusable("partition_table_sha256", "not a string", device_id)
        return None
    if _SHA256_HEX.fullmatch(value) is None:
        _unusable("partition_table_sha256", "not 64 lowercase hex characters", device_id)
        return None
    return value


def normalize_rollback_capable(value: object, *, device_id: str | None = None) -> bool | None:
    """`True` / `False` as reported, or `None` if the value is not a JSON boolean.

    `1`, `0`, `"true"` and `"false"` are not booleans here. This returns a real `bool`
    or `None`, so pydantic's lax `bool` coercion after a `mode="before"` validator has
    nothing left to coerce.
    """
    if value is None or isinstance(value, bool):
        return value
    _unusable("rollback_capable", "not a boolean", device_id)
    return None

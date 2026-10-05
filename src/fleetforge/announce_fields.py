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

This lives outside both `fleetforge.api` and `fleetforge.ingestor` because the API may
not import from the ingestor (the precedent is `identity.py`). It is named for
announced fields in general: R2b-be-6 adds the board measurements here.
"""

import logging
import unicodedata

logger = logging.getLogger(__name__)

# The 802.11 SSID limit, in bytes. Matches `agent/tools/ff_cfg.py::MAX_SSID_BYTES` and
# `ff_cfg.h`.
SSID_MAX_BYTES = 32

# A plausibility ceiling, not the protocol cap (4 today). A later writer may raise the
# cap, and its boards must still be stored.
KNOWN_NETWORKS_MAX = 64


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

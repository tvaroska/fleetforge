"""What a `device_id` is, and why the shape of it is a security control.

The eFuse MAC, lowercase hex, no separators — the same rule as the
`devices.device_id` CHECK. `db/models.py::Device`: "The format CHECK is therefore a
security control, not tidiness"; it is also the MQTT username the two `%u` pattern
ACLs (`pattern write ff/v1/d/%u/up/#`, `pattern read ff/v1/d/%u/dn/#`) depend on, so
a malformed one must never reach a write path *or* a broker credential.

**Never normalise, always reject.** Lowercasing an uppercase `device_id` here would
mean the string a caller signs up with and the string the ACL pattern binds to could
be reasoned about differently by two readers; the answer is a 4xx, not a `.lower()`.

This lived in `ingestor/protocol.py` until R0-be-4, which needs it in the API — and
`fleetforge.api` must not import from `fleetforge.ingestor` (same precedent as
`now_utc()` leaving `api/deps.py` for `clock.py`). Three callers read it now: the
ingestor's topic parser, `POST /v1/enroll`, and the broker provisioner's contract.
"""

import re

DEVICE_ID_RE = re.compile(r"^[0-9a-f]{12}$")


def is_valid_device_id(value: str) -> bool:
    """Is `value` a canonical device id (12 lowercase hex digits)?"""
    return DEVICE_ID_RE.match(value) is not None

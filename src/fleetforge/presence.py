"""Derived presence — one rule, every reader.

`spec/device-protocol.md` → *Presence — derived, never a socket state*:

* **`always_on`** → the retained `up/presence` value (and therefore the LWT) is
  authoritative. Offline within seconds of the socket dropping.
* **`sleepy`** → the LWT fires on every normal sleep and means nothing, so presence
  is `now - last_seen < tolerance × expected_wake_interval_s`.

Two rules no caller may re-derive:

1. **Presence is never stored.** `db/models.py::Device`: "Do not add an `online`
   column — it would be wrong the moment nothing writes to it." The ingestor puts a
   computed `online` in its `ff_events` payload, but that is a snapshot for the SSE
   consumer, not a cache.
2. **A sleepy device goes offline with no message arriving at all** — the timer
   simply expires. Which is exactly why every reader (`GET /v1/devices`) recomputes
   on read instead of trusting the last event it saw.

The tolerance is `config.Settings.presence_tolerance` and is passed in, so this
module needs neither the settings object nor a clock.
"""

import datetime as dt
import logging

from fleetforge.db.models import Device, PowerClass

logger = logging.getLogger(__name__)


def is_online(device: Device, *, now: dt.datetime, tolerance: float) -> bool:
    """Return whether `device` is considered online as of `now`.

    `tolerance` only applies to `sleepy` devices; `always_on` presence is the reported
    value, independent of `last_seen` (a board whose LWT fired is offline even if a
    message arrived a second ago).
    """
    if device.power_class == PowerClass.ALWAYS_ON:
        return device.presence_reported is True

    if device.power_class == PowerClass.SLEEPY:
        interval = device.expected_wake_interval_s
        # Guaranteed non-NULL and > 0 by the `sleepy_wake_interval` CHECK, but a
        # TypeError inside the ingestor's message loop is a worse failure than a
        # device reading offline, so defend anyway.
        if device.last_seen is None or interval is None or interval <= 0:
            return False
        return (now - device.last_seen) < dt.timedelta(seconds=tolerance * interval)

    # Unreachable: `devices.power_class` has a CHECK. Reaching it means the CHECK was
    # dropped or the row was built in memory, and both are bugs worth seeing.
    logger.warning(
        "device %s has undefined power_class %r; reporting offline",
        device.device_id,
        device.power_class,
    )
    return False

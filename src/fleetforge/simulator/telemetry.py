"""`up/telemetry` for a simulated board: `ff_telemetry.c`'s ring, in Python (R4-fw-2).

The payload is a proposal (`docs/features/health-telemetry.md` → *up/telemetry proposal*):
`{"dropped": n, "samples": [{"age_ms", "name", "value"}, ...]}`, oldest first, QoS 0, not
retained. A sample keeps the time it was taken on a monotonic clock, and `age_ms` is
measured when the payload is built, so the server's `receipt time - age_ms` puts a sample
taken while the board was offline where it belongs.

Import-pure like the rest of the simulator (`tests/test_simulator.py`): every constant is
retyped from the firmware and the spec proposal, never imported from the ingestor, and
`tests/test_telemetry.py` holds the copies together.

The firmware's design, kept on purpose:

* **drop-oldest** when full, counted in the next payload's `dropped`;
* **publish, then commit**: `next_payload()` removes nothing, `commit()` runs only after a
  publish went out, so a session that dies mid-publish loses no samples;
* one payload is at most `TELEMETRY_PAYLOAD_MAX` bytes, keys in the firmware's order, values
  printed with cJSON's round-trip rule (an integral value prints as `42`).
"""

import math
import re
import time
from collections import deque
from collections.abc import Callable
from dataclasses import dataclass

# Retyped from `ingestor/protocol.py::TELEMETRY_NAME_RE` (a tripwire test compares them) and
# `ff_telemetry.c`'s FF_TELEMETRY_* defines.
TELEMETRY_NAME_RE = re.compile(r"[a-z][a-z0-9_]{0,30}")
TELEMETRY_RESERVED_PREFIX = "ff_"
TELEMETRY_QOS = 0
TELEMETRY_CAPACITY = 64
TELEMETRY_FLUSH_S = 5.0
TELEMETRY_PAYLOAD_MAX = 900


def format_value(value: float) -> str:
    """`ff_telemetry.c::format_value`: `%.15g`, or `%.17g` when 15 digits do not round-trip."""
    text = f"{value:.15g}"
    if float(text) != value:
        text = f"{value:.17g}"
    return text


@dataclass(frozen=True, slots=True)
class _Sample:
    seq: int
    taken: float
    name: str
    value: float


class TelemetryBuffer:
    """The newest `capacity` samples not yet published, plus the `dropped` count.

    `clock` is injectable (seconds, monotonic) so a test can pin every `age_ms` exactly.
    """

    def __init__(
        self, clock: Callable[[], float] = time.monotonic, capacity: int = TELEMETRY_CAPACITY
    ) -> None:
        self._clock = clock
        self._capacity = capacity
        self._unsent: deque[_Sample] = deque()
        self._next_seq = 0
        self.dropped = 0

    def __len__(self) -> int:
        return len(self._unsent)

    def record(self, name: str, value: float) -> bool:
        """`ff_telemetry()`: False, recording nothing, for a bad name or a non-finite value."""
        if (
            not isinstance(name, str)
            or TELEMETRY_NAME_RE.fullmatch(name) is None
            or name.startswith(TELEMETRY_RESERVED_PREFIX)
        ):
            return False
        if isinstance(value, bool) or not isinstance(value, int | float):
            return False
        try:
            number = float(value)
        except OverflowError:
            return False
        if not math.isfinite(number):
            return False
        if len(self._unsent) >= self._capacity:
            self._unsent.popleft()
            self.dropped += 1
        self._unsent.append(_Sample(self._next_seq, self._clock(), name, number))
        self._next_seq += 1
        return True

    def next_payload(self) -> tuple[bytes, int, int] | None:
        """The next payload from the oldest unsent sample: `(bytes, last_seq,
        dropped_reported)`, or None when there is nothing to send. Removes nothing."""
        if not self._unsent:
            return None
        now = self._clock()  # once per payload, like the firmware
        head = f'{{"dropped":{self.dropped},"samples":['
        entries: list[str] = []
        size = len(head) + 2  # the closing `]}`
        last_seq = -1
        for sample in self._unsent:
            age_ms = max(int((now - sample.taken) * 1000), 0)
            entry = (
                f'{"," if entries else ""}{{"age_ms":{age_ms},"name":"{sample.name}",'
                f'"value":{format_value(sample.value)}}}'
            )
            if size + len(entry) > TELEMETRY_PAYLOAD_MAX:
                break
            entries.append(entry)
            size += len(entry)
            last_seq = sample.seq
        payload = (head + "".join(entries) + "]}").encode("ascii")
        return payload, last_seq, self.dropped

    def commit(self, last_seq: int, dropped_reported: int) -> None:
        """After a publish went out: forget what it carried, keep what came since."""
        while self._unsent and self._unsent[0].seq <= last_seq:
            self._unsent.popleft()
        self.dropped -= min(self.dropped, dropped_reported)

    def clear(self) -> None:
        """A reboot: the ring is RAM."""
        self._unsent.clear()
        self.dropped = 0

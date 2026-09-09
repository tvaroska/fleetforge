"""A simulated ESP32 board: the R0 device protocol with no hardware in the room.

`spec/device-protocol.md` is written for firmware, and until R0-fw-1 exists there is
nothing on the other end of the broker. This package is that other end — enough of a
board to enroll, connect, announce, hold presence and heartbeat, so that R0-be-2/3/4/5
and the dashboard can be exercised end to end (and so a regression in the retain or
QoS matrix is a failing test rather than a surprise on real silicon).

**It imports nothing from the server except `fleetforge.identity`.** Not
`fleetforge.config` (which would make `DATABASE_URL` mandatory to run a board), not
`fleetforge.db`, not the API or ingestor packages. A simulator that shares the
server's parsing is a simulator that agrees with the server by construction and
therefore proves nothing; `tests/test_simulator.py` enforces this with an AST
tripwire over every module here. `identity.DEVICE_ID_RE` is the deliberate exception:
the id format is a *contract*, not an implementation, and a board that made up its
own idea of it would be testing the wrong thing.

Layout:

* `device.py` — the protocol: topics, payloads, the QoS/retain matrix, the sessions.
* `state.py`  — the NVS analogue. **CRITICAL**: live broker credentials, 0600.
* `client.py` — the HTTPS half: enroll, and token issuance for `fleet`.
* `errors.py` — `SimulatorError`.
* `__main__.py` — the CLI (`just sim`, `just sim-fleet`).
"""

from fleetforge.simulator.device import (
    DeviceIdentity,
    LinkProfile,
    derive_device_id,
    dn_filter,
    mqtt_client_factory,
    run_always_on,
    run_session,
    run_sleepy,
    up_topic,
    will_for,
)
from fleetforge.simulator.errors import SimulatorError
from fleetforge.simulator.state import Credential

__all__ = [
    "Credential",
    "DeviceIdentity",
    "LinkProfile",
    "SimulatorError",
    "derive_device_id",
    "dn_filter",
    "mqtt_client_factory",
    "run_always_on",
    "run_session",
    "run_sleepy",
    "up_topic",
    "will_for",
]

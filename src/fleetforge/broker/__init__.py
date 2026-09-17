"""Broker credential provisioning — the seam between the API and Mosquitto.

`R0-be-4` created this package for `POST /v1/enroll`; `R0-sec-1` configures the broker
to match (`Settings.mqtt_dynsec_role` must equal the role name in
`dynamic-security.json`). R1-be-2 added the second seam alongside it: the `dn/cmd`
publisher now exists, and it is a *different privilege* — the `commander` client, which
may publish `ff/v1/d/+/dn/#` and may neither subscribe nor receive.

Import the Protocols and the errors from here; the adapters live next door:

* credentials — `broker/provisioner.py` (Null) and `broker/dynsec.py` (dynamic-security);
* commands — `broker/commands.py` (Protocol, builders, Null) and `broker/publisher.py`
  (aiomqtt).

`broker.dynsec` and `broker.publisher` are imported lazily by `api/deps.py`, so nothing
that only needs the Null paths pays for `aiomqtt`.
"""

from fleetforge.broker.commands import (
    CommandPublisher,
    CommandPublishError,
    NullCommandPublisher,
    command_topic,
    new_command_id,
    stage_payload,
)
from fleetforge.broker.provisioner import (
    DEVICE_ROLE,
    BrokerProvisioner,
    BrokerProvisioningError,
    NullProvisioner,
    generate_broker_password,
)

__all__ = [
    "DEVICE_ROLE",
    "BrokerProvisioner",
    "BrokerProvisioningError",
    "CommandPublishError",
    "CommandPublisher",
    "NullCommandPublisher",
    "NullProvisioner",
    "command_topic",
    "generate_broker_password",
    "new_command_id",
    "stage_payload",
]

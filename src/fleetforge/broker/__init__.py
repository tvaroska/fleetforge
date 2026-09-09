"""Broker credential provisioning — the seam between the API and Mosquitto.

`R0-be-4` created this package for `POST /v1/enroll`; `R0-sec-1` configures the broker
to match (`Settings.mqtt_dynsec_role` must equal the role name in
`dynamic-security.json`), and R1's `dn/cmd` publisher will live alongside it.

Import the Protocol and the errors from here; the adapters live in
`broker/provisioner.py` (Null) and `broker/dynsec.py` (Mosquitto dynamic-security).
`broker.dynsec` is imported lazily by `api/deps.get_broker_provisioner`, so nothing
that only needs the Null path pays for `aiomqtt`.
"""

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
    "NullProvisioner",
    "generate_broker_password",
]

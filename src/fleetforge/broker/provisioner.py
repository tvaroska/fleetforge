"""The broker-credential seam: one Protocol, two adapters. **CRITICAL.**

`CRITICAL.md` → *Mosquitto ACL configuration / dynsec provisioning*: "Two pattern
rules are the entire fleet authz. A wrong pattern lets any device impersonate any
other." This module is the *client* half of that row — the thing that creates the
broker client a device then connects as. `R0-sec-1` writes the broker half (the
`dynamic-security.json` that defines the role and its two pattern ACLs).

**The username is the `device_id`, and that is the security control.** The ACLs are
`pattern write ff/v1/d/%u/up/#` and `pattern read ff/v1/d/%u/dn/#`, where `%u` is the
MQTT username. Provision a username that is anything other than the validated
`device_id` — normalised, prefixed, suffixed — and those two patterns no longer
constrain what everyone reads them to constrain. Callers pass a `device_id` that has
already been through `fleetforge.identity.is_valid_device_id`.

**The password exists in exactly one place: the enrollment response body.** It is not
stored in Postgres, not cached, not logged. Dynsec keeps its own hash. Losing it means
re-enrolling, which is what `config.enroll_retry_window_s` exists for.

Two adapters, the same local-vs-real shape `R0-be-6` uses for MinIO/GCS:

* `NullProvisioner` — the anonymous dev broker (`mosquitto/conf.d/10-dev-anonymous.conf`).
  Honest, not a stub: it provisions nothing and says so, and the enrollment then leaves
  `devices.broker_provisioned_at` NULL.
* `DynsecProvisioner` (`broker/dynsec.py`) — the real one, over MQTT.
"""

import logging
import secrets
from typing import Protocol

logger = logging.getLogger(__name__)

# The dynsec role that carries the two `%u` pattern ACLs. The default for
# `Settings.mqtt_dynsec_role`; `R0-sec-1` must create a role with EXACTLY this name
# in `dynamic-security.json`, or every `createClient` fails and every enrollment
# answers 503.
DEVICE_ROLE = "device"

# 32 random bytes, base64url — the same strength as an `ffa_`/`ffe_` token secret.
BROKER_PASSWORD_BYTES = 32


class BrokerProvisioningError(RuntimeError):
    """The broker could not be made to hold this credential. Always a 503."""


def generate_broker_password() -> str:
    """A fresh per-device broker password. It exists once, in the response body."""
    return secrets.token_urlsafe(BROKER_PASSWORD_BYTES)


class BrokerProvisioner(Protocol):
    """Make a device's broker credential exist."""

    async def ensure_client(self, device_id: str, password: str) -> bool:
        """Make `device_id` a broker client with exactly `password` and the device role.

        **Idempotent by contract** — an enrollment retry (the grace window) calls this
        again for a username that already exists, and it must succeed.

        Returns `True` when the broker really holds the credential, so the caller may
        stamp `devices.broker_provisioned_at`; `False` when provisioning was *skipped*
        because no broker credential store exists yet (the R0 dev broker). Raises
        `BrokerProvisioningError` when it should have worked and did not.

        The tri-state is deliberate: "there is no credential store yet" is the expected
        R0 dev state, and `broker_provisioned_at IS NULL` is the column that records it
        truthfully — it is `R0-sec-1`'s reconcile list.
        """
        ...


class NullProvisioner:
    """Provisions nothing, because the dev broker authenticates nobody.

    `mosquitto/conf.d/10-dev-anonymous.conf` grants anonymous access and
    `$CONTROL/dynamic-security/v1` does nothing until `R0-sec-1` loads the plugin, so
    creating a client here would be a fiction. It returns `False` and the enrollment
    leaves `broker_provisioned_at` NULL — do not "helpfully" stamp the column, or the
    reconcile query `SELECT device_id FROM devices WHERE broker_provisioned_at IS NULL`
    silently skips every board enrolled before the broker was secured.
    """

    async def ensure_client(self, device_id: str, password: str) -> bool:
        """Log one WARNING (ids only, never the password) and provision nothing."""
        logger.warning(
            "broker provisioning skipped for %s: no dynsec credentials configured; "
            "the dev broker is anonymous (mosquitto/conf.d/10-dev-anonymous.conf). "
            "R0-sec-1 removes this path.",
            device_id,
        )
        return False

"""The broker-credential seam: one Protocol, two adapters. **CRITICAL.**

`CRITICAL.md` → *Mosquitto ACL configuration / dynsec provisioning*: "Two pattern
rules are the entire fleet authz. A wrong pattern lets any device impersonate any
other." This module is the *client* half of that row — the thing that creates the
broker client a device then connects as. The broker half is `mosquitto/`: dynsec
(`dynamic-security.json`, in the data volume) is **authentication only**, and the two
pattern ACLs live in **`mosquitto/acl`**, because Mosquitto 2.0's dynsec plugin has no
`%u` substitution (DECISIONS.md 2026-09-08, R0-sec-1).

**The username is the `device_id`, and that is the security control.** The ACLs are
`pattern write ff/v1/d/%u/up/#` and `pattern read ff/v1/d/%u/dn/#`, where `%u` is the
MQTT username. Provision a username that is anything other than the validated
`device_id` — normalised, prefixed, suffixed — and those two patterns no longer
constrain what everyone reads them to constrain. Callers pass a `device_id` that has
already been through `fleetforge.identity.is_valid_device_id`.

`python -m fleetforge.broker selftest` (`just broker-check`) proves the whole matrix
against a live broker; the Python suite can only check the files
(`tests/test_broker_config.py`).

**The password exists in exactly one place: the enrollment response body.** It is not
stored in Postgres, not cached, not logged. Dynsec keeps its own hash. Losing it means
re-enrolling, which is what `config.enroll_retry_window_s` exists for.

Two adapters, the same local-vs-real shape `R0-be-6` uses for MinIO/GCS:

* `NullProvisioner` — no dynsec control credential configured (the tests, a bare
  `create_app()`). Honest, not a stub: it provisions nothing and says so, and the
  enrollment then leaves `devices.broker_provisioned_at` NULL.
* `DynsecProvisioner` (`broker/dynsec.py`) — the real one, over MQTT.
"""

import logging
import secrets
from typing import Protocol

logger = logging.getLogger(__name__)

# The dynsec role every device's client is created with. The default for
# `Settings.mqtt_dynsec_role`; `mosquitto/bootstrap.sh` creates a role with EXACTLY
# this name, or every `createClient` fails and every enrollment answers 503.
#
# THAT ROLE IS INTENTIONALLY EMPTY, and it must stay empty. The two `%u` pattern ACLs
# live in `mosquitto/acl` because the dynsec plugin cannot express `%u` — verified
# against 2.0.22: a role holding `publishClientSend ff/v1/d/%u/up/#` denies the very
# client it names (MQTT v5 PUBACK 135). "Fixing" this by moving the patterns in here
# does not fail loudly; it silently denies the whole fleet.
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
    """Provisions nothing, because no dynsec control credential is configured.

    Selected only when `MQTT_DYNSEC_USERNAME`/`_PASSWORD` are unset — the tests and a
    bare `create_app()`. Never the dev stack or production: `docker-compose.yml` makes
    both mandatory (`${VAR:?}`) precisely so this path cannot be reached by accident.

    It returns `False` and the enrollment leaves `broker_provisioned_at` NULL — do not
    "helpfully" stamp the column, or the reconcile query `SELECT device_id FROM devices
    WHERE broker_provisioned_at IS NULL` silently skips every board that got a password
    the broker never stored. **Those boards cannot be reconciled**: the password only
    ever existed in the enrollment response, so the recovery is re-enrollment with a
    fresh `ffe_` token (`docs/runbooks/dev-stack.md`).
    """

    async def ensure_client(self, device_id: str, password: str) -> bool:
        """Log one WARNING (ids only, never the password) and provision nothing."""
        logger.warning(
            "broker provisioning skipped for %s: MQTT_DYNSEC_USERNAME/_PASSWORD are "
            "unset, so no broker client was created. The broker refuses anonymous "
            "clients, so this device cannot connect until it re-enrolls.",
            device_id,
        )
        return False

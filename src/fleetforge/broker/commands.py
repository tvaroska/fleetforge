"""The `dn/cmd` seam: one Protocol, two adapters, and the payload builders. **CRITICAL.**

R1-be-2. This is the *command* half of the broker package — `broker/provisioner.py` is
the credential half. Same two-module shape: the Protocol, the domain error, the pure
builders and the Null adapter live here with **no aiomqtt import**; the real adapter is
`broker/publisher.py` and is imported lazily by `api/deps.get_command_publisher`.

**Who is allowed to publish this.** Nothing in the estate could publish `dn/cmd` before
this task: dynsec default-denies `publishClientSend`, the `device` role is empty and the
dynsec *admin* credential is broker-root over `$CONTROL/...` only, which grants nothing
over `ff/v1/#`. `mosquitto/bootstrap.sh` therefore creates a third client — the
`commander` role, `publishClientSend ff/v1/d/+/dn/#` and **nothing else**: no
`subscribePattern`, no `publishClientReceive`, because the ingestor is the sole MQTT
subscriber and that invariant is load bearing. The device receives its own command
because `mosquitto/acl`'s `pattern read ff/v1/d/%u/dn/#` allows it and the two ACL
backends are OR-combined (allow wins). `just broker-check` proves the whole matrix.

**Known limitation, and it shapes every caller: under MQTT 3.1.1 a denied publish is
invisible.** The PUBACK carries no reason code, so paho/aiomqtt report success for a
message the broker silently dropped (`DECISIONS.md` 2026-09-08, R0-sec-1). A successful
`publish()` therefore means "the broker accepted the packet", never "the device got it".
The proof of delivery is the arriving `up/status`; the proof that the ACL is right is
`just broker-check`. Do not build a caller that reads the publish result as delivery.

**Retain is never set.** `spec/device-protocol.md` → *Retain and durability*: a retained
`dn/cmd` is replayed to the device on every reconnect, so a board that reboots after a
deploy would re-stage the same command forever. Durability comes from the device's
persistent session (`clean_session=false`), which is why the flag is a literal `False`
in `broker/publisher.py` rather than a parameter anything could pass wrongly.
"""

import json
import logging
import uuid
from typing import Any, Protocol

logger = logging.getLogger(__name__)

# `spec/device-protocol.md` → *Topic namespace*. The simulator retypes these locally
# (it may not import server modules); `tests/test_simulator.py` holds the tripwire.
TOPIC_ROOT = "ff/v1/d"
COMMAND_CHANNEL = "cmd"

# `spec/device-protocol.md` → *`dn/cmd` — commands*: the command vocabulary is
# `stage · apply · cancel · rollback · identify · reboot · set_cfg`. R1-be-2 ships
# exactly one of them.
COMMAND_STAGE = "stage"

# `spec/device-protocol.md`: "the device applies as soon as *it* judges the window
# safe" vs "the device stages and waits for an explicit `apply`". The `apply` command
# itself is R2 — a board given `on_command` sits in `staged` until then, by design.
APPLY_AUTO = "auto"
APPLY_ON_COMMAND = "on_command"
APPLY_MODES = (APPLY_AUTO, APPLY_ON_COMMAND)


class CommandPublishError(RuntimeError):
    """The command could not be handed to the broker. Always a 503."""


def new_command_id() -> str:
    """A fresh `dn/cmd` `id`.

    One id per deploy *intent*, not per publish: the device deduplicates on it
    (`spec/device-protocol.md`), so a retry of the same intent must carry the same id or
    the board downloads the same firmware twice. `deploys.py` owns that rule.
    """
    return uuid.uuid4().hex


def command_topic(device_id: str) -> str:
    """`ff/v1/d/{device_id}/dn/cmd` — the one topic a command is published to.

    `device_id` is the validated id, unnormalised: it is also the MQTT username the
    `%u` pattern ACL binds to, so a normalised spelling here would address a topic the
    board is not allowed to read (`broker/provisioner.py`).
    """
    return f"{TOPIC_ROOT}/{device_id}/dn/{COMMAND_CHANNEL}"


def stage_payload(
    *,
    cmd_id: str,
    url: str,
    sha256: str,
    size: int,
    version: str,
    apply: str,
    confirm_timeout_s: int,
) -> dict[str, Any]:
    """The `stage` command body, key for key as `spec/device-protocol.md` prints it.

    **Conform, do not extend.** Exactly `id`, `type`, `artifact{url,sha256,size,version}`,
    `apply`, `confirm_timeout_s` — no `partition_layout`, no `issued_at`, no `deploy_id`.
    The R0/R1 agent is flash-baked and parses what it was born with; an extra key is at
    best ignored and at worst a parse failure on a board that cannot be updated.

    **`artifact.sig` is deliberately absent.** The spec's example shows it, but R1 has no
    artifact signer, and emitting an empty or fake `sig` would teach a device to accept
    one. The signed URL *is* the authorization in R1 (`spec/prd.md` → *Security & data
    posture*); adding `sig` is a separate task, and a spec clarification is proposed for
    it in `docs/features/ota-deploy.md`.

    `confirm_timeout_s` arrives from `Settings.confirm_timeout_s`, never as a literal
    here: one number, one place.

    The returned dict carries the signed URL and is therefore a **credential**. It is
    published and discarded — never logged, never persisted, never echoed in an API
    response (`api/routers/deploys.py`).
    """
    return {
        "id": cmd_id,
        "type": COMMAND_STAGE,
        "artifact": {
            "url": url,
            "sha256": sha256,
            "size": size,
            "version": version,
        },
        "apply": apply,
        "confirm_timeout_s": confirm_timeout_s,
    }


def encode_command(payload: dict[str, Any]) -> bytes:
    """Compact UTF-8 JSON, the same encoding the simulator and the agent decode.

    Compact for the same reason as `simulator/device.py::encode`: the payload crosses a
    constrained link and CBOR must stay a drop-in for V3.
    """
    return json.dumps(payload, separators=(",", ":")).encode("utf-8")


class CommandPublisher(Protocol):
    """Hand one command to the broker for one device."""

    async def publish(self, device_id: str, payload: dict[str, Any]) -> None:
        """Publish `payload` to `ff/v1/d/{device_id}/dn/cmd`, QoS 1, **never retained**.

        Returns None on success and raises `CommandPublishError` on anything else. As
        the module docstring says: success means the broker accepted the packet, not
        that the device received it — MQTT 3.1.1 cannot tell the difference.
        """
        ...


class NullCommandPublisher:
    """Publishes nothing and says so — by **raising**.

    Selected only when `MQTT_COMMAND_USERNAME`/`_PASSWORD` are unset
    (`api/deps.py::mqtt_command_configured`): the tests and a bare `create_app()`.
    `docker-compose.yml` makes both mandatory (`${VAR:?}`) so the dev stack and
    production cannot reach this path by accident.

    **Deliberately unlike `NullProvisioner`, which succeeds silently.** An enrolment
    against no broker is still a usable dev flow — it leaves an honest
    `broker_provisioned_at IS NULL`. A deploy that publishes nothing has no such
    honest trace: answering 202 would put a `requested` row in the KPI history and a
    "deploying" banner on the dashboard for a command no board will ever see.
    """

    async def publish(self, device_id: str, payload: dict[str, Any]) -> None:
        """Log one WARNING (ids only, never the payload) and raise."""
        logger.warning(
            "deploy command for %s not published: MQTT_COMMAND_USERNAME/_PASSWORD are "
            "unset, so no broker publisher is configured.",
            device_id,
        )
        raise CommandPublishError(
            "no broker command credential is configured; the command was not published"
        )

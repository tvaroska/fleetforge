"""The `dn/cmd` seam: one Protocol, two adapters, and the payload builders. **CRITICAL.**

R1-be-2. This is the *command* half of the broker package — `broker/provisioner.py` is
the credential half. Same two-module shape: the Protocol, the domain error, the pure
builders and the Null adapter live here with **no aiomqtt import**; the real adapter is
`broker/publisher.py` and is imported lazily by `api/deps.get_command_publisher`.

**Who is allowed to publish this.** Nothing in the estate could publish `dn/cmd` before
this task: dynsec default-denies `publishClientSend`, the `device` role is empty and the
dynsec *admin* credential is broker-root over `$CONTROL/...` only, which grants nothing
over `ff/v1/#`. `mosquitto/configure.sh` therefore creates a third client — the
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
CFG_CHANNEL = "cfg"

# `spec/device-protocol.md` → *`dn/cmd` — commands*: the command vocabulary is
# `stage · apply · cancel · rollback · identify · reboot · set_cfg`. The server sends
# five of them: `stage` (R1-be-2) and the four verbs below (R5-be-2, R5-spec-1 Patch A2).
# `identify` and `set_cfg` are not sent by anything.
COMMAND_STAGE = "stage"
COMMAND_APPLY = "apply"
COMMAND_CANCEL = "cancel"
COMMAND_ROLLBACK = "rollback"
COMMAND_REBOOT = "reboot"
VERB_COMMANDS = (COMMAND_APPLY, COMMAND_CANCEL, COMMAND_ROLLBACK, COMMAND_REBOOT)
# The two verbs that name a transaction (`cmd_id`, the stage's `id`), Patch A2 / D1.
_VERBS_WITH_CMD_ID = (COMMAND_APPLY, COMMAND_CANCEL)

# `spec/device-protocol.md`: "the device applies as soon as *it* judges the window
# safe" vs "the device stages and waits for an explicit `apply`". The `apply` verb is
# R5-be-2's, and `/deploy` refuses `on_command` to a board that does not announce it
# (`deploy_precheck.apply_refusals`): such a board would sit at `staged` until it restarts.
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


def cfg_topic(device_id: str) -> str:
    """`ff/v1/d/{device_id}/dn/cfg` — retained device configuration topic (CUJ-2)."""
    return f"{TOPIC_ROOT}/{device_id}/dn/{CFG_CHANNEL}"


def cfg_payload(*, secrets_rev: int = 0, stay_awake: bool = False) -> dict[str, Any]:
    """The retained `dn/cfg` body (`spec/device-protocol.md` → *`dn/cfg`*)."""
    return {
        "proto": 1,
        "secrets_rev": secrets_rev,
        "stay_awake": stay_awake,
    }


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


def verb_payload(*, command_id: str, type: str, cmd_id: str | None = None) -> dict[str, Any]:
    """An `apply` / `cancel` / `rollback` / `reboot` body, exactly R5-spec-1 Patch A2.

    `{"id","type","cmd_id"}` for `apply` and `cancel` (the transaction they act on), and
    `{"id","type"}` for `rollback` and `reboot` — no `cmd_id` key at all, not a null: the
    patch prints them that way and the agent answers a `cmd_id`-less apply/cancel with
    `cmd_id missing`. Keys in that order. Nothing secret: a verb carries no URL.

    Raises `ValueError` for an unknown type, an `apply`/`cancel` without `cmd_id`, or a
    `rollback`/`reboot` given one: a wrong shape is a server bug, never sent.
    """
    if type not in VERB_COMMANDS:
        raise ValueError(f"not a verb this server sends: {type!r}")
    if type in _VERBS_WITH_CMD_ID:
        if not cmd_id:
            raise ValueError(f"{type} names a transaction: cmd_id is required")
        return {"id": command_id, "type": type, "cmd_id": cmd_id}
    if cmd_id is not None:
        raise ValueError(f"{type} names no transaction: cmd_id must be None")
    return {"id": command_id, "type": type}


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

    async def publish_cfg(self, device_id: str, payload: dict[str, Any]) -> None:
        """Publish `payload` to `ff/v1/d/{device_id}/dn/cfg`, QoS 1, **retained**."""
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

    async def publish_cfg(self, device_id: str, payload: dict[str, Any]) -> None:
        """Log one WARNING and raise when no broker credential is configured."""
        logger.warning(
            "dn/cfg for %s not published: MQTT_COMMAND_USERNAME/_PASSWORD are unset.",
            device_id,
        )
        raise CommandPublishError(
            "no broker command credential is configured; dn/cfg was not published"
        )

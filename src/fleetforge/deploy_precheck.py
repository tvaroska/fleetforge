"""Every reason a deploy is refused or warned about. R2b-be-2.

The one home of these sentences, shared by `POST /v1/devices/{id}/deploy` (which turns the
**first** refusal into its HTTP error) and `POST /v1/devices/{id}/deploy/precheck` (which
returns them all, plus the warnings, without sending anything).

Not `deploycheck.py`: `fleetforge/deploycheck.py` is the unrelated S0-infra-9 release gate
(`just deploy-check`).

Pure: takes the `Device` row, the resolved artifact and the derived `online` flag. It never
reads settings, a clock or the DB, and never re-derives presence (`presence.py` owns that).

Refusals cannot be overridden. Warnings are reported and never block a send: an offline
board is still deployed to, because the QoS-1 command waits in its persistent session.
The `detail` text of a refused deploy is lifted verbatim into the dashboard banner, so do
not reword a sentence without reading `tests/test_api_deploy.py`.
"""

from dataclasses import dataclass

from fleetforge.db.models import Device, PowerClass

OTA_CAPABILITY = "ota"

# Stable codes: clients branch on these, never on the message.
NO_ARTIFACT_FOR_TARGET = "no_artifact_for_target"
LAYOUT_MISMATCH = "layout_mismatch"
SLOT_TOO_SMALL = "slot_too_small"
NO_OTA_CAPABILITY = "no_ota_capability"
NEVER_CONNECTED = "never_connected"
OFFLINE = "offline"
SLEEPY = "sleepy"


@dataclass(frozen=True, slots=True)
class ResolvedArtifact:
    """The bytes a label names, for one chip. Read once, used by every check."""

    sha256: str
    size_bytes: int
    partition_layout: str | None


@dataclass(frozen=True, slots=True)
class Finding:
    code: str
    message: str


def refusals(device: Device, artifact: ResolvedArtifact | None, *, version: str) -> list[Finding]:
    """Every reason this board cannot take this label, in the order a deploy checks them.

    `artifact` is `None` when the label names nothing for the device's chip. The
    capability check does not need an artifact, so it still runs; layout and slot size are
    skipped. Layout and slot size are only checked when both sides are known: an R0 board
    that announced neither is not refused for being old.
    """
    found: list[Finding] = []

    if artifact is None:
        found.append(
            Finding(
                NO_ARTIFACT_FOR_TARGET,
                f"no {device.platform_type} artifact labelled {version}. "
                "The target is this device's chip, not a choice: upload the image "
                f"built for {device.platform_type} under that version.",
            )
        )
    else:
        if (
            device.partition_layout is not None
            and artifact.partition_layout is not None
            and device.partition_layout != artifact.partition_layout
        ):
            found.append(
                Finding(
                    LAYOUT_MISMATCH,
                    f"this device runs partition layout {device.partition_layout} and "
                    f"{version} was built for {artifact.partition_layout}. "
                    "An image written into the wrong partition table does not boot.",
                )
            )
        if device.ota_slot_size is not None and artifact.size_bytes > device.ota_slot_size:
            found.append(
                Finding(
                    SLOT_TOO_SMALL,
                    f"{version} is {artifact.size_bytes} bytes and this device's OTA slot "
                    f"is {device.ota_slot_size}. The image must fit the slot it is "
                    "written into.",
                )
            )

    if OTA_CAPABILITY not in device.capabilities:
        found.append(
            Finding(
                NO_OTA_CAPABILITY,
                "this device did not announce the `ota` capability, so it has no agent "
                "that can stage an update. It announced: "
                f"{', '.join(device.capabilities) or 'nothing'}.",
            )
        )
    return found


def warnings(device: Device, *, online: bool) -> list[Finding]:
    """What the operator should know before sending. Never blocks a send."""
    found: list[Finding] = []

    if device.last_seen is None and device.presence_reported is None:
        found.append(
            Finding(
                NEVER_CONNECTED,
                "this board has never connected to the broker, so there is no queue to "
                "hold the update: it would not receive it on its first boot. Send it "
                "once the board is online.",
            )
        )
    elif not online:
        found.append(
            Finding(
                OFFLINE,
                "this board is offline: the update waits in its queue and starts when "
                "the board reconnects.",
            )
        )

    if device.power_class == PowerClass.SLEEPY:
        interval = device.expected_wake_interval_s
        cadence = f"about every {interval} s" if interval is not None else "on a schedule"
        found.append(
            Finding(
                SLEEPY,
                f"this board sleeps between wakes ({cadence}): the update waits in its "
                "queue and starts at its next wake.",
            )
        )
    return found

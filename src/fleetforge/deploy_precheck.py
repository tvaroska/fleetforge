"""Every reason a deploy is refused or warned about. R2b-be-2.

The one home of these sentences, shared by `POST /v1/devices/{id}/deploy` (which turns the
**first** refusal into its HTTP error) and `POST /v1/devices/{id}/deploy/precheck` (which
returns them all, plus the warnings, without sending anything).

Not `deploycheck.py`: `fleetforge/deploycheck.py` is the unrelated S0-infra-9 release gate
(`just deploy-check`).

Pure: takes the `Device` row, the resolved artifact and the derived `online` flag. It never
reads settings, a clock or the DB, and never re-derives presence (`presence.py` owns that).

Refusals cannot be overridden, not even by `override`. Warnings are reported and never
block a send: an offline board is still deployed to, because the QoS-1 command waits in
its persistent session. **Except gating warnings** (`needs_override`, R2b-be-7):
`/deploy` answers them with a 409 carrying the warning's own sentence unless the body
names their code in `override`. Per code, never a blanket `force`; the pre-check reports
them regardless of `override`, so the dashboard can render the tick.

The board measurements are fail-open (R2b-be-6): a NULL `partition_table_sha256` never
refuses and a NULL `rollback_capable` never warns. Every board before its first OTA, and
every agent at or below 0.4.6, reports NULL, and an old board is not refused for being old.

The `detail` text of a refused deploy is lifted verbatim into the dashboard banner, so do
not reword a sentence without reading `tests/test_api_deploy.py`.
"""

import logging
from collections.abc import Collection
from dataclasses import dataclass

from fleetforge.db.models import Device, PowerClass
from fleetforge.firmware.manifest import SUPPORTED_LAYOUTS
from fleetforge.merged_image import MergedImage

logger = logging.getLogger(__name__)

OTA_CAPABILITY = "ota"

# Stable codes: clients branch on these, never on the message.
NO_ARTIFACT_FOR_TARGET = "no_artifact_for_target"
LAYOUT_MISMATCH = "layout_mismatch"
SLOT_TOO_SMALL = "slot_too_small"
PARTITION_TABLE_MISMATCH = "partition_table_mismatch"
NO_OTA_CAPABILITY = "no_ota_capability"
NEVER_CONNECTED = "never_connected"
OFFLINE = "offline"
SLEEPY = "sleepy"
MERGED_BINARY = "merged_binary"  # refused at upload, not by refusals()
ROLLBACK_INCAPABLE = "rollback_incapable"

# The warnings `/deploy` enforces unless `override` names them. `api/schemas.py`'s
# `OverrideCode` literal is kept equal to this by `tests/test_deploy_precheck.py`.
GATING_CODES: tuple[str, ...] = (ROLLBACK_INCAPABLE,)


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
    # A gating warning: `/deploy` refuses it (409) unless `override` lists `code`.
    needs_override: bool = False


def merged_binary(merged: MergedImage) -> Finding:
    """The upload refusal for a merged full-flash image. Plain text, no backticks."""
    return Finding(
        MERGED_BINARY,
        "this file is a merged full-flash image, not an app image: it carries "
        f"{' and '.join(merged.evidence)}. A merged image is written over USB from offset "
        "0x0; an OTA update writes only the app, into a slot. Upload the app .bin from the "
        "same build instead: idf.py build/<project>.bin, Arduino <sketch>.ino.bin (not "
        ".ino.merged.bin), PlatformIO .pio/build/<env>/firmware.bin, ESPHome "
        "firmware.ota.bin (not firmware.factory.bin).",
    )


def refusals(device: Device, artifact: ResolvedArtifact | None, *, version: str) -> list[Finding]:
    """Every reason this board cannot take this label, in the order a deploy checks them.

    `artifact` is `None` when the label names nothing for the device's chip. The
    capability check does not need an artifact, so it still runs; layout and slot size are
    skipped. Layout and slot size are only checked when both sides are known: an R0 board
    that announced neither is not refused for being old.

    The partition-table fingerprint is a property of the device against its own profile,
    not of the artifact, so it runs without one too. It is checked only when the device
    sent a fingerprint AND its layout has a known one (`SUPPORTED_LAYOUTS`): a board that
    has not reported one yet is not refused for being old, and an unknown layout is
    logged, not guessed at.
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

    announced = device.partition_table_sha256
    layout = device.partition_layout
    if announced is not None and layout is not None:
        profile = SUPPORTED_LAYOUTS.get(layout)
        expected = profile.partition_table_sha256 if profile is not None else None
        if expected is None:
            logger.info(
                "device %s announces a partition table fingerprint for layout %s, which has "
                "no known fingerprint; not checked",
                device.device_id,
                layout,
            )
        elif announced != expected:
            found.append(
                Finding(
                    PARTITION_TABLE_MISMATCH,
                    f"this device announces partition layout {layout} but its partition "
                    f"table fingerprint is {announced}, not the {expected} that {layout} "
                    "has. The device disagrees with its profile, so an image built for "
                    f"{layout} could be written over the wrong partitions.",
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
    """What the operator should know before sending.

    Never blocks a send, except the gating warnings (`needs_override`), which come last
    and which `/deploy` enforces through `unmet_gates`.
    """
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

    found.extend(gating_warnings(device))
    return found


def gating_warnings(device: Device) -> list[Finding]:
    """The warnings `/deploy` refuses unless overridden. Pure, NULL-silent.

    `rollback_capable is False`, never `not rollback_capable`: `None` (no OTA observed yet,
    or an agent too old to report) must stay silent.
    """
    if device.rollback_capable is False:
        return [
            Finding(
                ROLLBACK_INCAPABLE,
                "this board's bootloader cannot roll back: it booted an earlier update "
                "without arming the safety net. If this build fails to boot or never "
                "reconnects, the board stays broken until someone reflashes it over USB. "
                "Send it only if losing this board to a bad build is acceptable.",
                needs_override=True,
            )
        ]
    return []


def unmet_gates(device: Device, override: Collection[str]) -> list[Finding]:
    """The gating warnings `override` does not name, in order. Empty means the gate opens."""
    return [f for f in gating_warnings(device) if f.code not in override]

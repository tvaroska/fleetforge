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

A layout this server does not support is refused, `unsupported_layout` (R3-fw-5): a board
whose table matches no layout its firmware knows announces `unknown`
(`UNKNOWN_PARTITION_LAYOUT`), and the sentence names the layout the build expects, its slot
size and the fix. A partition table never changes over the air, so the fix is always a USB
flash, and the sentence says so.

**The supported layouts are a `LayoutCatalog` (R3-be-2), loaded from the
`partition_profiles` table by the caller and passed in**, so this module stays pure: it
never reads the DB. `catalog` is a required keyword, so no production path can silently
fall back to the builtins. A board's effective layout is `catalog.resolve(...)` (DECISIONS
R3-be-2 D7): the id it announces when that id is an adopted profile, otherwise the adopted
profile whose fingerprint it announces. That is how a board announcing `unknown` becomes
deployable once an operator has named its map. A board whose map is recorded as a pending
detected profile is still refused as `unsupported_layout`, and the sentence gains one
sentence naming the adoption route.

The `detail` text of a refused deploy is lifted verbatim into the dashboard banner, so do
not reword a sentence without reading `tests/test_api_deploy.py`.
"""

import logging
from collections.abc import Collection
from dataclasses import dataclass

from fleetforge.db.models import Device, PowerClass
from fleetforge.merged_image import MergedImage
from fleetforge.partition_profiles import LayoutCatalog

logger = logging.getLogger(__name__)

OTA_CAPABILITY = "ota"

# Stable codes: clients branch on these, never on the message.
NO_ARTIFACT_FOR_TARGET = "no_artifact_for_target"
LAYOUT_MISMATCH = "layout_mismatch"
UNSUPPORTED_LAYOUT = "unsupported_layout"
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

# Where a library user finds the partitions.csv for each builtin layout: the fix named by
# `unsupported_layout` and `partition_table_mismatch`. `tests/test_deploy_precheck.py` keeps
# its keys equal to BUILTIN_LAYOUTS', so a new builtin cannot ship without a hint. An
# operator-defined profile has no entry: the operator knows where its table came from.
LAYOUT_SOURCES: dict[str, str] = {
    "ab-4m-v1": (
        "the library's examples/basic_idf/partitions.csv, or the prebuilt agent from the "
        "browser flasher"
    ),
    "ab-4m-arduino-v1": "the library's examples/Basic/partitions.csv, beside the sketch",
}


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


def _and(items: list[str]) -> str:
    """`a`, `a and b`, `a, b and c`."""
    return items[0] if len(items) == 1 else f"{', '.join(items[:-1])} and {items[-1]}"


def _fix_for(layouts: list[str]) -> str:
    """The one fix there is: a table never changes over the air, so it is a USB flash."""
    if len(layouts) == 1:
        hint = LAYOUT_SOURCES.get(layouts[0])
        csv = f"the partitions.csv for {layouts[0]}" + (f" ({hint})" if hint else "")
    else:
        hints = "; ".join(
            f"{layout}: {LAYOUT_SOURCES.get(layout, 'the partitions.csv it was defined from')}"
            for layout in layouts
        )
        csv = f"the partitions.csv for one of them ({hints})"
    return f"build it with {csv}, flash it once over USB, then deploy again."


def _supported_summary(catalog: LayoutCatalog) -> str:
    """Every supported layout with its slot size, for a sentence with no known target."""
    return _and(
        [
            f"{layout} (two OTA slots of {profile.ota_slot_size} bytes each)"
            for layout, profile in catalog.layouts.items()
        ]
    )


def _unsupported_layout(
    device: Device, artifact: ResolvedArtifact | None, version: str, catalog: LayoutCatalog
) -> Finding:
    """The board announces a layout this server does not support (R3-fw-5). One cause, one
    sentence: what it announces, what the build expects, and the fix. No backticks."""
    announced = [f"partition layout {device.partition_layout}"]
    if device.ota_slot_size is not None:
        announced.append(f"an OTA slot of {device.ota_slot_size} bytes")
    if device.partition_table_sha256 is not None:
        announced.append(f"partition table fingerprint {device.partition_table_sha256}")
    target = artifact.partition_layout if artifact is not None else None
    if target is not None and target in catalog.layouts:
        expected = (
            f"{version} was built for {target}, whose two OTA slots are "
            f"{catalog.layouts[target].ota_slot_size} bytes each."
        )
        layouts = [target]
    else:
        expected = f"This server supports {_supported_summary(catalog)}."
        layouts = list(catalog.layouts)
    # R3-be-2: the old sentence stays a byte-for-byte prefix; a pending map only appends.
    adoptable = ""
    if device.partition_table_sha256 is not None and device.partition_table_sha256 in (
        catalog.pending
    ):
        adoptable = (
            " This flash map is recorded as a detected profile (fingerprint "
            f"{device.partition_table_sha256}): an operator can adopt it by naming it, and "
            "images uploaded for that name can then be deployed to this board."
        )
    return Finding(
        UNSUPPORTED_LAYOUT,
        "this device's flash map is not a layout this server supports: it announces "
        f"{_and(announced)}. {expected} A partition table never changes over the air, so no "
        f"deploy can fix this board: {_fix_for(layouts)}{adoptable}",
    )


def refusals(
    device: Device,
    artifact: ResolvedArtifact | None,
    *,
    version: str,
    catalog: LayoutCatalog,
) -> list[Finding]:
    """Every reason this board cannot take this label, in the order a deploy checks them.

    `artifact` is `None` when the label names nothing for the device's chip. The
    capability check does not need an artifact, so it still runs; layout and slot size are
    skipped. Layout and slot size are only checked when both sides are known: an R0 board
    that announced neither is not refused for being old.

    The board's layout is its *effective* one, `catalog.resolve(...)` (R3-be-2 D7): the
    announced id when it is an adopted profile, otherwise the adopted profile its
    fingerprint matches. A board that announces a layout and resolves to none (`unknown`
    included) is refused, `unsupported_layout`, with or without an artifact and whatever
    layout the artifact claims (R3-fw-5). It replaces `layout_mismatch` for that board, and
    its fingerprint is not checked: there is no profile to check it against.

    The partition-table fingerprint is a property of the device against its own profile,
    not of the artifact, so it runs without one too. It is checked only when the announced
    id itself is an adopted profile, the device sent a fingerprint AND that profile has a
    known one: a board that has not reported one yet is not refused for being old. A board
    resolved *by* its fingerprint matches by construction, so it is not checked again.
    """
    found: list[Finding] = []
    layout = device.partition_layout
    effective = catalog.resolve(layout, device.partition_table_sha256)
    unsupported = layout is not None and effective is None

    if artifact is None:
        found.append(
            Finding(
                NO_ARTIFACT_FOR_TARGET,
                f"no {device.platform_type} artifact labelled {version}. "
                "The target is this device's chip, not a choice: upload the image "
                f"built for {device.platform_type} under that version.",
            )
        )
    if unsupported:
        found.append(_unsupported_layout(device, artifact, version, catalog))
    if artifact is not None:
        if (
            not unsupported
            and effective is not None
            and artifact.partition_layout is not None
            and effective != artifact.partition_layout
        ):
            found.append(
                Finding(
                    LAYOUT_MISMATCH,
                    f"this device runs partition layout {effective} and "
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
    profile = catalog.layouts.get(layout) if layout is not None else None
    if announced is not None and layout is not None and profile is not None:
        expected = profile.partition_table_sha256
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
                    f"{layout} could be written over the wrong partitions. {layout} has two "
                    f"OTA slots of {profile.ota_slot_size} bytes each. A partition table "
                    f"never changes over the air: {_fix_for([layout])}",
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

"""`deploy_precheck.py` — the pure home of every deploy refusal and warning. R2b-be-2."""

import re
import typing
from pathlib import Path

import pytest

from fleetforge.api.schemas import OverrideCode
from fleetforge.clock import now_utc
from fleetforge.db.models import Device
from fleetforge.deploy_precheck import (
    GATING_CODES,
    LAYOUT_SOURCES,
    ResolvedArtifact,
    merged_binary,
    refusals,
    unmet_gates,
    warnings,
)
from fleetforge.firmware.manifest import (
    BUILTIN_LAYOUTS,
    UNKNOWN_PARTITION_LAYOUT,
    LayoutProfile,
)
from fleetforge.merged_image import MergedImage
from fleetforge.partition_profiles import LayoutCatalog, builtin_catalog
from tests.conftest import capture_logs

LAYOUT = "ab-4m-v1"
# spec/device-protocol.md → Partition layouts, retyped.
AB_SHA = "1fa67e6bbd034e434d04e9d6f4f52bbe899361602cd498573eb3bde97d1559ed"
ARDUINO_SHA = "05528998ae17fb6a7a5741443f9a7a4720c766f370fefc30814cbc3e391c1fc4"
ARDUINO = "ab-4m-arduino-v1"
# tests/fixtures/wrong-layout-partitions.csv: slots of 1835008, matches no supported layout.
WRONG_SHA = "47db53920359cfb4581532a293d8e563401f3abe37f9b282cc713039ac937c4c"
DEVICE_PROTOCOL = Path(__file__).resolve().parent.parent / "spec" / "device-protocol.md"
# What a freshly migrated `partition_profiles` table holds (R3-be-2).
BUILTIN = builtin_catalog()
# An operator's own map, adopted as `my-map` (R3-be-2): what WRONG_SHA becomes once named.
MY_MAP = "my-map"
ADOPTED = LayoutCatalog(
    layouts={**BUILTIN.layouts, MY_MAP: LayoutProfile(1835008, WRONG_SHA)},
    by_fingerprint={**BUILTIN.by_fingerprint, WRONG_SHA: MY_MAP},
    pending=frozenset(),
)
# The same map recorded as a detected profile nobody has named yet.
PENDING = LayoutCatalog(
    layouts=BUILTIN.layouts, by_fingerprint=BUILTIN.by_fingerprint, pending=frozenset({WRONG_SHA})
)


def device(**overrides: object) -> Device:
    values: dict[str, object] = {
        "device_id": "a4cf12b3de90",
        "platform_type": "esp32",
        "link_type": "wifi",
        "power_class": "always_on",
        "fw_version": "1.4.2",
        "partition_layout": LAYOUT,
        "ota_slot_size": 1000,
        "capabilities": ["ota"],
        "presence_reported": True,
        "last_seen": now_utc(),
    }
    values.update(overrides)
    return Device(**values)


def artifact(
    size: int = 500, layout: str | None = LAYOUT, has_lib_marker: bool | None = None
) -> ResolvedArtifact:
    return ResolvedArtifact(
        sha256="ab" * 32, size_bytes=size, partition_layout=layout, has_lib_marker=has_lib_marker
    )


def codes(found: list) -> list[str]:  # type: ignore[type-arg]
    return [f.code for f in found]


class TestRefusals:
    def test_a_clean_board_has_none(self) -> None:
        assert refusals(device(), artifact(), version="1.5.0", catalog=BUILTIN) == []

    def test_no_artifact(self) -> None:
        assert codes(refusals(device(), None, version="1.5.0", catalog=BUILTIN)) == [
            "no_artifact_for_target"
        ]

    def test_capability_is_still_checked_without_an_artifact(self) -> None:
        found = refusals(device(capabilities=[]), None, version="1.5.0", catalog=BUILTIN)
        assert codes(found) == ["no_artifact_for_target", "no_ota_capability"]

    def test_all_three_in_order_and_naming_their_values(self) -> None:
        found = refusals(
            device(partition_layout=ARDUINO, ota_slot_size=100, capabilities=["x"]),
            artifact(size=500),
            version="1.5.0",
            catalog=BUILTIN,
        )
        assert codes(found) == ["layout_mismatch", "slot_too_small", "no_ota_capability"]
        assert ARDUINO in found[0].message and LAYOUT in found[0].message
        assert "500" in found[1].message and "100" in found[1].message
        assert "x" in found[2].message
        assert (
            "nothing"
            in refusals(device(capabilities=[]), artifact(), version="1", catalog=BUILTIN)[
                0
            ].message
        )

    def test_an_r0_board_is_not_refused_for_being_old(self) -> None:
        d = device(partition_layout=None, ota_slot_size=None)
        assert refusals(d, artifact(size=10**9), version="1.5.0", catalog=BUILTIN) == []


class TestWarnings:
    def test_online_always_on_is_quiet(self) -> None:
        assert warnings(device(), online=True, artifact=None) == []

    def test_offline(self) -> None:
        assert codes(warnings(device(), online=False, artifact=None)) == ["offline"]

    def test_never_connected_replaces_offline(self) -> None:
        d = device(last_seen=None, presence_reported=None)
        assert codes(warnings(d, online=False, artifact=None)) == ["never_connected"]

    def test_sleepy_online(self) -> None:
        found = warnings(
            device(power_class="sleepy", expected_wake_interval_s=60), online=True, artifact=None
        )
        assert codes(found) == ["sleepy"]
        assert "60" in found[0].message

    def test_sleepy_offline_has_both(self) -> None:
        d = device(power_class="sleepy", expected_wake_interval_s=60)
        assert codes(warnings(d, online=False, artifact=None)) == ["offline", "sleepy"]


class TestMergedBinary:
    def test_the_sentence_names_what_was_seen_and_what_to_pick(self) -> None:
        evidence = ("the bootloader at 0x1000", "the partition table at 0x8000")
        finding = merged_binary(MergedImage(evidence))
        assert finding.code == "merged_binary"
        assert "merged full-flash image" in finding.message
        assert "the bootloader at 0x1000 and the partition table at 0x8000" in finding.message
        for needle in ("app .bin", "firmware.ota.bin", ".ino.merged.bin"):
            assert needle in finding.message
        assert "`" not in finding.message

    def test_refusals_is_unchanged(self) -> None:
        assert refusals(device(), artifact(), version="1.5.0", catalog=BUILTIN) == []


class TestPartitionTableFingerprint:
    """R2b-be-7: device vs its own profile. Present AND known AND different refuses."""

    def test_the_matching_fingerprint_is_not_refused(self) -> None:
        d = device(partition_table_sha256=AB_SHA)
        assert refusals(d, artifact(), version="1.5.0", catalog=BUILTIN) == []

    def test_another_layouts_fingerprint_is_refused_naming_layout_and_both_hashes(self) -> None:
        found = refusals(
            device(partition_table_sha256=ARDUINO_SHA), artifact(), version="1.5.0", catalog=BUILTIN
        )
        assert codes(found) == ["partition_table_mismatch"]
        message = found[0].message
        assert LAYOUT in message and ARDUINO_SHA in message and AB_SHA in message
        assert "partition table fingerprint" in message
        assert "`" not in message

    def test_the_mismatch_sentence_keeps_its_text_and_names_the_fix(self) -> None:
        """R3-fw-5: the library 0.4.7 case (compiled id, wrong table) now says what to do."""
        found = refusals(
            device(partition_table_sha256=WRONG_SHA), artifact(), version="1.5.0", catalog=BUILTIN
        )
        message = found[0].message
        assert message.startswith(
            f"this device announces partition layout {LAYOUT} but its partition table "
            f"fingerprint is {WRONG_SHA}, not the {AB_SHA} that {LAYOUT} has. The device "
            f"disagrees with its profile, so an image built for {LAYOUT} could be written "
            "over the wrong partitions. "
        )
        assert "1966080" in message and LAYOUT_SOURCES[LAYOUT] in message
        assert "USB" in message and "never changes over the air" in message
        assert "`" not in message

    def test_no_fingerprint_is_fail_open(self) -> None:
        d = device(partition_table_sha256=None)
        assert refusals(d, artifact(), version="1.5.0", catalog=BUILTIN) == []

    def test_no_layout_is_fail_open(self) -> None:
        d = device(partition_layout=None, partition_table_sha256=WRONG_SHA)
        assert refusals(d, artifact(), version="1.5.0", catalog=BUILTIN) == []

    def test_no_layout_with_a_known_fingerprint_resolves_to_its_profile(self) -> None:
        """R3-be-2 D7: the table proves the map even when no id was announced. Before, an
        id-less board was never layout-checked; now its fingerprint names its layout."""
        d = device(partition_layout=None, partition_table_sha256=ARDUINO_SHA)
        assert refusals(d, artifact(layout=ARDUINO), version="1.5.0", catalog=BUILTIN) == []
        found = refusals(d, artifact(), version="1.5.0", catalog=BUILTIN)
        assert codes(found) == ["layout_mismatch"]
        assert f"runs partition layout {ARDUINO}" in found[0].message

    def test_an_unsupported_layout_is_refused_not_fingerprint_checked(self) -> None:
        """R3-fw-5: it used to be logged and let through. There is no profile to check the
        fingerprint against, so it is refused once, and nothing is logged as 'not checked'."""
        d = device(partition_layout="single-2m-v1", partition_table_sha256=WRONG_SHA)
        with capture_logs("fleetforge.deploy_precheck") as records:
            found = refusals(d, artifact(layout=None), version="1.5.0", catalog=BUILTIN)
        assert codes(found) == ["unsupported_layout"]
        lines = [r.getMessage() for r in records]
        assert not any("not checked" in line for line in lines), lines

    def test_an_unknown_id_on_a_known_table_resolves_by_fingerprint(self) -> None:
        """R3-be-2 D7: an id the server does not know, on a table it does, is that table's
        layout. (Before, it was refused as unsupported.) Real firmware detects the id by
        fingerprint, so this is an old or foreign build announcing its own name."""
        d = device(partition_layout="single-2m-v1", partition_table_sha256=ARDUINO_SHA)
        assert refusals(d, artifact(layout=ARDUINO), version="1.5.0", catalog=BUILTIN) == []
        found = refusals(d, artifact(layout=None), version="1.5.0", catalog=BUILTIN)
        assert found == []

    def test_a_profile_with_no_known_fingerprint_is_not_checked(self) -> None:
        catalog = LayoutCatalog(
            layouts={LAYOUT: LayoutProfile(1966080, None)}, by_fingerprint={}, pending=frozenset()
        )
        d = device(partition_table_sha256=ARDUINO_SHA)
        assert refusals(d, artifact(), version="1.5.0", catalog=catalog) == []

    def test_the_arduino_profile_matches_its_own_fingerprint(self) -> None:
        d = device(partition_layout="ab-4m-arduino-v1", partition_table_sha256=ARDUINO_SHA)
        assert (
            refusals(d, artifact(layout="ab-4m-arduino-v1"), version="1.5.0", catalog=BUILTIN) == []
        )

    def test_it_runs_without_an_artifact(self) -> None:
        found = refusals(
            device(partition_table_sha256=ARDUINO_SHA), None, version="1.5.0", catalog=BUILTIN
        )
        assert codes(found) == ["no_artifact_for_target", "partition_table_mismatch"]

    def test_the_full_order(self) -> None:
        d = device(ota_slot_size=100, capabilities=["x"], partition_table_sha256=ARDUINO_SHA)
        found = refusals(
            d, artifact(size=500, layout="ab-4m-arduino-v1"), version="1.5.0", catalog=BUILTIN
        )
        assert codes(found) == [
            "layout_mismatch",
            "slot_too_small",
            "partition_table_mismatch",
            "no_ota_capability",
        ]
        assert all(f.needs_override is False for f in found)

    def test_a_rollback_incapable_board_is_not_refused(self) -> None:
        assert (
            refusals(device(rollback_capable=False), artifact(), version="1.5.0", catalog=BUILTIN)
            == []
        )


class TestUnsupportedLayout:
    """R3-fw-5: a board whose table matches no layout its firmware knows announces
    `unknown`. It is refused, naming what it announced, what the build expects and the fix."""

    def unknown(self, **overrides: object) -> Device:
        values: dict[str, object] = {
            "partition_layout": UNKNOWN_PARTITION_LAYOUT,
            "ota_slot_size": 1835008,
            "partition_table_sha256": WRONG_SHA,
        }
        values.update(overrides)
        return device(**values)

    def test_the_reserved_id_is_never_supported(self) -> None:
        assert UNKNOWN_PARTITION_LAYOUT == "unknown"
        assert UNKNOWN_PARTITION_LAYOUT not in BUILTIN_LAYOUTS

    def test_it_names_the_announcement_the_expected_layout_and_the_fix(self) -> None:
        found = refusals(
            self.unknown(), artifact(size=500, layout=ARDUINO), version="1.5.0", catalog=BUILTIN
        )
        assert codes(found) == ["unsupported_layout"]
        message = found[0].message
        for needle in (
            "partition layout unknown",
            "1835008",
            WRONG_SHA,
            "1.5.0 was built for ab-4m-arduino-v1",
            "1966080",
            "examples/Basic/partitions.csv",
            "USB",
        ):
            assert needle in message, needle
        assert "`" not in message
        assert found[0].needs_override is False

    def test_unannounced_measurements_are_left_out_of_the_sentence(self) -> None:
        d = self.unknown(ota_slot_size=None, partition_table_sha256=None)
        message = refusals(d, artifact(layout=ARDUINO), version="1.5.0", catalog=BUILTIN)[0].message
        assert "it announces partition layout unknown. " in message
        assert "fingerprint" not in message and "an OTA slot of" not in message

    def test_without_an_artifact_it_names_every_supported_layout(self) -> None:
        found = refusals(self.unknown(), None, version="1.5.0", catalog=BUILTIN)
        assert codes(found) == ["no_artifact_for_target", "unsupported_layout"]
        message = found[1].message
        for layout, profile in BUILTIN_LAYOUTS.items():
            assert layout in message and LAYOUT_SOURCES[layout] in message
            assert str(profile.ota_slot_size) in message
        assert "`" not in message

    def test_an_artifact_with_no_layout_is_still_refused(self) -> None:
        """Before R3-fw-5 an unknown-layout board plus a layout-less artifact passed every
        check."""
        found = refusals(self.unknown(), artifact(layout=None), version="1.5.0", catalog=BUILTIN)
        assert codes(found) == ["unsupported_layout"]
        assert all(layout in found[0].message for layout in BUILTIN_LAYOUTS)

    def test_an_artifact_for_an_unsupported_layout_names_the_supported_ones(self) -> None:
        found = refusals(
            self.unknown(), artifact(layout="single-2m-v1"), version="1.5.0", catalog=BUILTIN
        )
        assert codes(found) == ["unsupported_layout"]
        assert all(layout in found[0].message for layout in BUILTIN_LAYOUTS)

    def test_it_replaces_layout_mismatch_and_keeps_the_rest(self) -> None:
        found = refusals(
            self.unknown(ota_slot_size=100, capabilities=["x"]),
            artifact(size=500, layout=LAYOUT),
            version="1.5.0",
            catalog=BUILTIN,
        )
        assert codes(found) == ["unsupported_layout", "slot_too_small", "no_ota_capability"]
        assert all(f.needs_override is False for f in found)

    def test_any_unsupported_id_is_refused_not_just_unknown(self) -> None:
        found = refusals(
            device(partition_layout="single-2m-v1"), artifact(), version="1.5.0", catalog=BUILTIN
        )
        assert codes(found) == ["unsupported_layout"]

    def test_every_builtin_layout_has_a_source_hint(self) -> None:
        assert LAYOUT_SOURCES.keys() == BUILTIN_LAYOUTS.keys()
        assert all("partitions.csv" in hint for hint in LAYOUT_SOURCES.values())
        assert not any("`" in hint for hint in LAYOUT_SOURCES.values())


class TestOperatorProfiles:
    """R3-be-2: the catalog is the `partition_profiles` table. A board announcing `unknown`
    is resolved by its fingerprint: deployable once its map is adopted, refused while it is
    pending, and the pending refusal names the adoption route."""

    def unknown(self, **overrides: object) -> Device:
        values: dict[str, object] = {
            "partition_layout": UNKNOWN_PARTITION_LAYOUT,
            "ota_slot_size": 1835008,
            "partition_table_sha256": WRONG_SHA,
        }
        values.update(overrides)
        return device(**values)

    def test_an_adopted_fingerprint_makes_an_unknown_board_deployable(self) -> None:
        found = refusals(
            self.unknown(), artifact(size=500, layout=MY_MAP), version="1.5.0", catalog=ADOPTED
        )
        assert found == []

    def test_it_is_layout_checked_under_its_adopted_name(self) -> None:
        found = refusals(self.unknown(), artifact(size=500), version="1.5.0", catalog=ADOPTED)
        assert codes(found) == ["layout_mismatch"]
        assert found[0].message.startswith(
            f"this device runs partition layout {MY_MAP} and 1.5.0 was built for {LAYOUT}."
        )

    def test_a_pending_map_is_refused_and_names_the_adoption_route(self) -> None:
        old = refusals(self.unknown(), artifact(layout=ARDUINO), version="1.5.0", catalog=BUILTIN)
        found = refusals(self.unknown(), artifact(layout=ARDUINO), version="1.5.0", catalog=PENDING)
        assert codes(found) == ["unsupported_layout"]
        message = found[0].message
        assert message.startswith(old[0].message), "the old sentence must stay a prefix"
        assert message[len(old[0].message) :] == (
            f" This flash map is recorded as a detected profile (fingerprint {WRONG_SHA}): an "
            "operator can adopt it by naming it, and images uploaded for that name can then "
            "be deployed to this board."
        )
        assert "`" not in message

    def test_an_unrecorded_map_keeps_the_old_sentence_exactly(self) -> None:
        found = refusals(self.unknown(), None, version="1.5.0", catalog=BUILTIN)
        assert codes(found) == ["no_artifact_for_target", "unsupported_layout"]
        assert "detected profile" not in found[1].message
        assert found[1].message.endswith("flash it once over USB, then deploy again.")

    def test_a_builtin_id_with_an_adopted_users_fingerprint_is_a_mismatch(self) -> None:
        """The announced id wins (D7): the board claims ab-4m-v1 and carries my-map."""
        d = device(partition_table_sha256=WRONG_SHA)
        found = refusals(d, artifact(), version="1.5.0", catalog=ADOPTED)
        assert codes(found) == ["partition_table_mismatch"]

    def test_a_board_resolved_by_fingerprint_is_not_fingerprint_checked_again(self) -> None:
        with capture_logs("fleetforge.deploy_precheck") as records:
            found = refusals(
                self.unknown(), artifact(layout=MY_MAP), version="1.5.0", catalog=ADOPTED
            )
        assert found == []
        assert not any("not checked" in r.getMessage() for r in records)

    def test_an_unsupported_artifact_layout_names_operator_profiles_without_a_hint(self) -> None:
        """The multi-layout fix lists every adopted profile; an operator profile has no
        LAYOUT_SOURCES hint, so it is not echoed back as its own hint ("my-map: my-map")."""
        d = device(partition_layout="single-2m-v1", partition_table_sha256=None)
        found = refusals(d, artifact(layout=None), version="1.5.0", catalog=ADOPTED)
        assert codes(found) == ["unsupported_layout"]
        message = found[0].message
        assert f"{MY_MAP} (two OTA slots of 1835008 bytes each)" in message
        assert f"{MY_MAP}: the partitions.csv it was defined from" in message
        assert f"{MY_MAP}: {MY_MAP}" not in message
        for layout in BUILTIN_LAYOUTS:
            assert f"{layout}: {LAYOUT_SOURCES[layout]}" in message

    def test_a_single_operator_layout_has_no_hint(self) -> None:
        found = refusals(
            device(partition_layout="single-2m-v1"),
            artifact(layout=MY_MAP),
            version="1.5.0",
            catalog=ADOPTED,
        )
        assert found[0].message.endswith(
            f"build it with the partitions.csv for {MY_MAP}, flash it once over USB, then "
            "deploy again."
        )

    def test_resolve(self) -> None:
        assert ADOPTED.resolve(LAYOUT, None) == LAYOUT
        assert ADOPTED.resolve(UNKNOWN_PARTITION_LAYOUT, WRONG_SHA) == MY_MAP
        assert ADOPTED.resolve(None, WRONG_SHA) == MY_MAP
        assert ADOPTED.resolve(LAYOUT, WRONG_SHA) == LAYOUT
        assert PENDING.resolve(UNKNOWN_PARTITION_LAYOUT, WRONG_SHA) is None
        assert BUILTIN.resolve(None, None) is None

    def test_the_builtin_catalog_is_the_builtin_layouts(self) -> None:
        assert dict(BUILTIN.layouts) == BUILTIN_LAYOUTS
        assert BUILTIN.pending == frozenset()
        assert BUILTIN.by_fingerprint == {AB_SHA: LAYOUT, ARDUINO_SHA: ARDUINO}


class TestRollbackIncapable:
    """R2b-be-7: the gating warning. Only `False` warns; `None` stays silent."""

    def test_false_is_a_gating_warning(self) -> None:
        found = warnings(device(rollback_capable=False), online=True, artifact=None)
        assert codes(found) == ["rollback_incapable"]
        assert found[0].needs_override is True
        assert "cannot roll back" in found[0].message
        assert "`" not in found[0].message

    @pytest.mark.parametrize("value", [True, None])
    def test_true_and_unknown_are_silent(self, value: bool | None) -> None:
        assert warnings(device(rollback_capable=value), online=True, artifact=None) == []
        assert unmet_gates(device(rollback_capable=value), [], artifact=None) == []

    def test_gating_goes_last(self) -> None:
        d = device(power_class="sleepy", expected_wake_interval_s=60, rollback_capable=False)
        found = warnings(d, online=False, artifact=None)
        assert codes(found) == ["offline", "sleepy", "rollback_incapable"]
        assert [f.needs_override for f in found] == [False, False, True]

    def test_plain_warnings_do_not_need_an_override(self) -> None:
        d = device(last_seen=None, presence_reported=None)
        assert all(f.needs_override is False for f in warnings(d, online=False, artifact=None))

    def test_unmet_gates_is_cleared_only_by_naming_the_code(self) -> None:
        d = device(rollback_capable=False)
        assert codes(unmet_gates(d, [], artifact=None)) == ["rollback_incapable"]
        assert codes(unmet_gates(d, ["something_else"], artifact=None)) == ["rollback_incapable"]
        assert unmet_gates(d, ["rollback_incapable"], artifact=None) == []
        assert unmet_gates(d, [], artifact=None) == [warnings(d, online=True, artifact=None)[0]]


class TestNoLibraryMarker:
    """R3-be-1: the image's gating warning. Only a stored `False` warns; `None` (an artifact
    uploaded before the scan existed) and no artifact stay silent."""

    def test_false_is_a_gating_warning(self) -> None:
        found = warnings(device(), online=True, artifact=artifact(has_lib_marker=False))
        assert codes(found) == ["no_library_marker"]
        assert found[0].needs_override is True
        assert "library" in found[0].message
        assert "`" not in found[0].message

    @pytest.mark.parametrize("value", [True, None])
    def test_marked_and_unknown_are_silent(self, value: bool | None) -> None:
        a = artifact(has_lib_marker=value)
        assert warnings(device(), online=True, artifact=a) == []
        assert unmet_gates(device(), [], artifact=a) == []

    def test_no_artifact_is_silent(self) -> None:
        assert warnings(device(), online=True, artifact=None) == []

    def test_both_gates_in_order_and_last(self) -> None:
        d = device(power_class="sleepy", expected_wake_interval_s=60, rollback_capable=False)
        found = warnings(d, online=False, artifact=artifact(has_lib_marker=False))
        assert codes(found) == ["offline", "sleepy", "rollback_incapable", "no_library_marker"]
        assert [f.needs_override for f in found] == [False, False, True, True]
        assert tuple(codes(found[2:])) == GATING_CODES

    def test_unmet_gates_is_cleared_only_by_naming_the_code(self) -> None:
        a = artifact(has_lib_marker=False)
        assert codes(unmet_gates(device(), [], artifact=a)) == ["no_library_marker"]
        assert codes(unmet_gates(device(), ["rollback_incapable"], artifact=a)) == [
            "no_library_marker"
        ]
        assert unmet_gates(device(), ["no_library_marker"], artifact=a) == []

    def test_both_raised_need_both_named(self) -> None:
        d = device(rollback_capable=False)
        a = artifact(has_lib_marker=False)
        assert codes(unmet_gates(d, [], artifact=a)) == ["rollback_incapable", "no_library_marker"]
        assert codes(unmet_gates(d, ["no_library_marker"], artifact=a)) == ["rollback_incapable"]
        assert codes(unmet_gates(d, ["rollback_incapable"], artifact=a)) == ["no_library_marker"]
        assert unmet_gates(d, ["rollback_incapable", "no_library_marker"], artifact=a) == []


class TestLayoutProfiles:
    """`BUILTIN_LAYOUTS` is the spec's *Partition layouts* table, row for row."""

    def test_the_code_table_is_the_spec_table(self) -> None:
        rows = re.findall(
            r"^\| `([a-z0-9-]+)` \| (\d+) \| `([0-9a-f]{64})` \|",
            DEVICE_PROTOCOL.read_text(),
            re.MULTILINE,
        )
        assert len(rows) >= 2, "the Partition layouts table did not parse"
        spec = {layout: (int(slot), sha) for layout, slot, sha in rows}
        code = {
            layout: (profile.ota_slot_size, profile.partition_table_sha256)
            for layout, profile in BUILTIN_LAYOUTS.items()
        }
        assert spec == code


class TestOverrideCodes:
    def test_the_schema_literal_is_the_gating_codes(self) -> None:
        assert set(typing.get_args(OverrideCode)) == set(GATING_CODES)

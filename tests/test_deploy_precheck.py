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
    ResolvedArtifact,
    merged_binary,
    refusals,
    unmet_gates,
    warnings,
)
from fleetforge.firmware.manifest import SUPPORTED_LAYOUTS, LayoutProfile
from fleetforge.merged_image import MergedImage
from tests.conftest import capture_logs

LAYOUT = "ab-4m-v1"
# spec/device-protocol.md → Partition layouts, retyped.
AB_SHA = "1fa67e6bbd034e434d04e9d6f4f52bbe899361602cd498573eb3bde97d1559ed"
ARDUINO_SHA = "05528998ae17fb6a7a5741443f9a7a4720c766f370fefc30814cbc3e391c1fc4"
DEVICE_PROTOCOL = Path(__file__).resolve().parent.parent / "spec" / "device-protocol.md"


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


def artifact(size: int = 500, layout: str | None = LAYOUT) -> ResolvedArtifact:
    return ResolvedArtifact(sha256="ab" * 32, size_bytes=size, partition_layout=layout)


def codes(found: list) -> list[str]:  # type: ignore[type-arg]
    return [f.code for f in found]


class TestRefusals:
    def test_a_clean_board_has_none(self) -> None:
        assert refusals(device(), artifact(), version="1.5.0") == []

    def test_no_artifact(self) -> None:
        assert codes(refusals(device(), None, version="1.5.0")) == ["no_artifact_for_target"]

    def test_capability_is_still_checked_without_an_artifact(self) -> None:
        found = refusals(device(capabilities=[]), None, version="1.5.0")
        assert codes(found) == ["no_artifact_for_target", "no_ota_capability"]

    def test_all_three_in_order_and_naming_their_values(self) -> None:
        found = refusals(
            device(partition_layout="single-2m-v1", ota_slot_size=100, capabilities=["x"]),
            artifact(size=500),
            version="1.5.0",
        )
        assert codes(found) == ["layout_mismatch", "slot_too_small", "no_ota_capability"]
        assert "single-2m-v1" in found[0].message and LAYOUT in found[0].message
        assert "500" in found[1].message and "100" in found[1].message
        assert "x" in found[2].message
        assert "nothing" in refusals(device(capabilities=[]), artifact(), version="1")[0].message

    def test_an_r0_board_is_not_refused_for_being_old(self) -> None:
        d = device(partition_layout=None, ota_slot_size=None)
        assert refusals(d, artifact(size=10**9), version="1.5.0") == []


class TestWarnings:
    def test_online_always_on_is_quiet(self) -> None:
        assert warnings(device(), online=True) == []

    def test_offline(self) -> None:
        assert codes(warnings(device(), online=False)) == ["offline"]

    def test_never_connected_replaces_offline(self) -> None:
        d = device(last_seen=None, presence_reported=None)
        assert codes(warnings(d, online=False)) == ["never_connected"]

    def test_sleepy_online(self) -> None:
        found = warnings(device(power_class="sleepy", expected_wake_interval_s=60), online=True)
        assert codes(found) == ["sleepy"]
        assert "60" in found[0].message

    def test_sleepy_offline_has_both(self) -> None:
        d = device(power_class="sleepy", expected_wake_interval_s=60)
        assert codes(warnings(d, online=False)) == ["offline", "sleepy"]


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
        assert refusals(device(), artifact(), version="1.5.0") == []


class TestPartitionTableFingerprint:
    """R2b-be-7: device vs its own profile. Present AND known AND different refuses."""

    def test_the_matching_fingerprint_is_not_refused(self) -> None:
        d = device(partition_table_sha256=AB_SHA)
        assert refusals(d, artifact(), version="1.5.0") == []

    def test_another_layouts_fingerprint_is_refused_naming_layout_and_both_hashes(self) -> None:
        found = refusals(device(partition_table_sha256=ARDUINO_SHA), artifact(), version="1.5.0")
        assert codes(found) == ["partition_table_mismatch"]
        message = found[0].message
        assert LAYOUT in message and ARDUINO_SHA in message and AB_SHA in message
        assert "partition table fingerprint" in message
        assert "`" not in message

    def test_no_fingerprint_is_fail_open(self) -> None:
        d = device(partition_table_sha256=None)
        assert refusals(d, artifact(), version="1.5.0") == []

    def test_no_layout_is_fail_open(self) -> None:
        d = device(partition_layout=None, partition_table_sha256=ARDUINO_SHA)
        assert refusals(d, artifact(), version="1.5.0") == []

    def test_an_unknown_layout_is_logged_not_checked(self) -> None:
        d = device(partition_layout="single-2m-v1", partition_table_sha256=ARDUINO_SHA)
        with capture_logs("fleetforge.deploy_precheck") as records:
            found = refusals(d, artifact(layout=None), version="1.5.0")
        assert found == []
        lines = [r.getMessage() for r in records]
        assert any("not checked" in line and "single-2m-v1" in line for line in lines), lines

    def test_a_profile_with_no_known_fingerprint_is_not_checked(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        monkeypatch.setitem(SUPPORTED_LAYOUTS, LAYOUT, LayoutProfile(1966080, None))
        d = device(partition_table_sha256=ARDUINO_SHA)
        assert refusals(d, artifact(), version="1.5.0") == []

    def test_the_arduino_profile_matches_its_own_fingerprint(self) -> None:
        d = device(partition_layout="ab-4m-arduino-v1", partition_table_sha256=ARDUINO_SHA)
        assert refusals(d, artifact(layout="ab-4m-arduino-v1"), version="1.5.0") == []

    def test_it_runs_without_an_artifact(self) -> None:
        found = refusals(device(partition_table_sha256=ARDUINO_SHA), None, version="1.5.0")
        assert codes(found) == ["no_artifact_for_target", "partition_table_mismatch"]

    def test_the_full_order(self) -> None:
        d = device(ota_slot_size=100, capabilities=["x"], partition_table_sha256=ARDUINO_SHA)
        found = refusals(d, artifact(size=500, layout="ab-4m-arduino-v1"), version="1.5.0")
        assert codes(found) == [
            "layout_mismatch",
            "slot_too_small",
            "partition_table_mismatch",
            "no_ota_capability",
        ]
        assert all(f.needs_override is False for f in found)

    def test_a_rollback_incapable_board_is_not_refused(self) -> None:
        assert refusals(device(rollback_capable=False), artifact(), version="1.5.0") == []


class TestRollbackIncapable:
    """R2b-be-7: the gating warning. Only `False` warns; `None` stays silent."""

    def test_false_is_a_gating_warning(self) -> None:
        found = warnings(device(rollback_capable=False), online=True)
        assert codes(found) == ["rollback_incapable"]
        assert found[0].needs_override is True
        assert "cannot roll back" in found[0].message
        assert "`" not in found[0].message

    @pytest.mark.parametrize("value", [True, None])
    def test_true_and_unknown_are_silent(self, value: bool | None) -> None:
        assert warnings(device(rollback_capable=value), online=True) == []
        assert unmet_gates(device(rollback_capable=value), []) == []

    def test_gating_goes_last(self) -> None:
        d = device(power_class="sleepy", expected_wake_interval_s=60, rollback_capable=False)
        found = warnings(d, online=False)
        assert codes(found) == ["offline", "sleepy", "rollback_incapable"]
        assert [f.needs_override for f in found] == [False, False, True]

    def test_plain_warnings_do_not_need_an_override(self) -> None:
        d = device(last_seen=None, presence_reported=None)
        assert all(f.needs_override is False for f in warnings(d, online=False))

    def test_unmet_gates_is_cleared_only_by_naming_the_code(self) -> None:
        d = device(rollback_capable=False)
        assert codes(unmet_gates(d, [])) == ["rollback_incapable"]
        assert codes(unmet_gates(d, ["something_else"])) == ["rollback_incapable"]
        assert unmet_gates(d, ["rollback_incapable"]) == []
        assert unmet_gates(d, []) == [warnings(d, online=True)[0]]


class TestLayoutProfiles:
    """`SUPPORTED_LAYOUTS` is the spec's *Partition layouts* table, row for row."""

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
            for layout, profile in SUPPORTED_LAYOUTS.items()
        }
        assert spec == code


class TestOverrideCodes:
    def test_the_schema_literal_is_the_gating_codes(self) -> None:
        assert set(typing.get_args(OverrideCode)) == set(GATING_CODES)

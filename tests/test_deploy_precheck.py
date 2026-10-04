"""`deploy_precheck.py` — the pure home of every deploy refusal and warning. R2b-be-2."""

from fleetforge.clock import now_utc
from fleetforge.db.models import Device
from fleetforge.deploy_precheck import ResolvedArtifact, refusals, warnings

LAYOUT = "ab-4m-v1"


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

"""`scripts/selftest_window_e2e.py` (R5-test-1): the simulated self-test / safe-window harness.

The harness itself runs against the dev stack (`just selftest-window-e2e`); what is held here
on every `just test` is what it retypes from the project (it is stdlib-only and imports
nothing), the bytes it uploads, its checkers, and the vacuity hooks.
"""

from __future__ import annotations

import importlib.util
import re
import sys
from pathlib import Path
from types import ModuleType

import pytest

from fleetforge.lib_marker import find_marker
from fleetforge.simulator import device as sim

REPO_ROOT = Path(__file__).resolve().parent.parent
SCRIPT = REPO_ROOT / "scripts" / "selftest_window_e2e.py"
JUSTFILE = REPO_ROOT / "justfile"


def _load() -> ModuleType:
    """By path, like the other script tests; registered first because it uses dataclasses."""
    spec = importlib.util.spec_from_file_location("selftest_window_e2e", SCRIPT)
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    sys.modules["selftest_window_e2e"] = module
    spec.loader.exec_module(module)
    return module


e2e = _load()


@pytest.mark.parametrize("name", ["sw6ad2ba84v-01", "sim-01", "x"])
def test_the_device_id_is_the_simulators(name: str) -> None:
    assert e2e.derive_device_id(name) == sim.derive_device_id(name)


def test_the_built_image_carries_the_label_and_a_marker_the_server_reads() -> None:
    head = e2e.APP_HEAD.read_bytes()
    labels = e2e.labels_for("6ad2ba84")
    for label in labels.values():
        image = e2e.build_image(head, label)
        field = image[e2e.VERSION_AT : e2e.VERSION_AT + e2e.VERSION_LEN]
        assert field.rstrip(b"\0") == label.encode()
        marker = find_marker(image)
        assert marker is not None and marker.lib_version == "0.0.0-e2e"
        assert len(label.encode()) < e2e.VERSION_LEN
    assert labels["stfail"].endswith(e2e.STFAIL_MARKER)
    assert len(set(labels.values())) == len(labels)
    with pytest.raises(ValueError):
        e2e.build_image(head, "x" * 32)


def test_the_retyped_words_are_the_simulators() -> None:
    assert e2e.SELFTEST_PASSED == sim.SELFTEST_PASSED
    assert e2e.SELFTEST_FAIL_REASON == sim.SELFTEST_FAIL_REASON
    assert e2e.DETAIL_APPLY_WAITS == sim.DETAIL_APPLY_WAITS
    assert e2e.failed_detail("ota_1") == (
        f"{sim.SELFTEST_FAILED}{sim.SELFTEST_FAIL_REASON}{sim.RETURNED_TO}ota_1"
    )
    assert set(e2e.R5_CAPABILITIES) == {*sim.VERB_COMMANDS, "ota", sim.CAPABILITY_SELFTEST}


def test_every_scenario_has_a_function_and_is_a_break_id() -> None:
    assert tuple(e2e.SCENARIO_FUNCTIONS) == e2e.SCENARIOS
    assert len(e2e.SCENARIOS) == 7
    source = SCRIPT.read_text()
    for scenario in e2e.SCENARIOS:
        assert f'run.broken("{scenario}")' in source, scenario


def test_an_unknown_break_id_is_a_setup_failure(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    monkeypatch.setenv("E2E_BREAK", "nope")
    assert e2e.main(["http://127.0.0.1:9", str(tmp_path / "out")]) == 2
    assert not (tmp_path / "out").exists()  # refused before anything is written


def test_steps_in_order() -> None:
    walk = [
        "requested",
        "staging",
        "downloading",
        "verifying",
        "staged",
        "awaiting_safe_window",
        "applying",
        "rebooting",
        "confirming",
        "confirmed",
    ]
    assert e2e.steps_in_order(walk, e2e.WAIT_ORDER)
    assert not e2e.steps_in_order([s for s in walk if s != "awaiting_safe_window"], e2e.WAIT_ORDER)
    swapped = list(walk)
    swapped[5], swapped[6] = swapped[6], swapped[5]
    assert not e2e.steps_in_order(swapped, e2e.WAIT_ORDER)
    assert not e2e.steps_in_order([], e2e.WAIT_ORDER)


def test_check_selftest() -> None:
    failed = e2e.failed_selftest("ota_1")
    assert e2e.check_selftest({"selftest": failed}, failed) is None
    assert e2e.check_selftest({"selftest": e2e.PASSED}, e2e.PASSED) is None
    assert e2e.check_selftest({"selftest": None}, None) is None
    assert e2e.check_selftest({"selftest": e2e.PASSED}, failed) is not None
    assert e2e.check_selftest({"selftest": {**failed, "returned_to": "ota_0"}}, failed) is not None
    assert e2e.check_selftest({}, e2e.PASSED) is not None
    assert e2e.check_selftest(None, e2e.PASSED) is not None


def test_find_secrets() -> None:
    password = "hunter2-correct-horse"  # noqa: S105 - a fixture value
    assert (
        e2e.find_secrets(
            "login    admin session acquired (the ffa_ token is never printed)", password
        )
        == []
    )
    assert e2e.find_secrets(f"oops {password}", password) == ["the admin password"]
    assert e2e.find_secrets("ffe_AbCdEfGhIjKlMnOpQrStUv", password) == ["a ffe_ token"]
    assert e2e.find_secrets("ffa_0123456789abcdef0123", password) == ["a ffa_ token"]


def test_the_justfile_recipe_runs_the_script() -> None:
    text = JUSTFILE.read_text()
    assert re.search(
        r"^selftest-window-e2e \*args:\n    python3 -u scripts/selftest_window_e2e\.py \{\{ args \}\}$",
        text,
        re.M,
    )


def test_the_script_is_stdlib_only() -> None:
    """It never imports the project (mypy sees scripts/ as a package of its own)."""
    source = SCRIPT.read_text()
    assert not re.search(r"^\s*(from|import)\s+(fleetforge|scripts|lib_quickstart)\b", source, re.M)

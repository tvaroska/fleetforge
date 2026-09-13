"""The agent's power posture and its flash budget. No toolchain, no broker, no database.

Deliberately NOT in `test_agent_partitions.py`. That file guards **flash-time
immutables** — the options whose mistakes cost "a van and a screwdriver" — and its value
comes from every line in it being that class of mistake. Everything here is recoverable
by OTA: a wrong CPU frequency or a lost `-Os` is a bad image, not a recall. Mixing the
two would dilute the one file whose failures are physical.

What these tests are for is narrower and still worth having: **three decisions that are
invisible in their own effect.** An agent built at 160 MHz behaves identically to one
built at 80 MHz except for a current draw nobody measures; an image built at `-Og`
behaves identically to `-Os` until an OTA slot fills up two releases later; a board
without an explicit `esp_wifi_set_ps` call still sleeps, because the SDK default happens
to agree with us today. Each one can be silently undone by a config bump, an IDF pin
bump, or a merge, and nothing at runtime would say so.

The size budget is the only test here that reads a built artefact, and it skips when
there is no bundle — a clean clone has an empty `agent/dist/` on purpose
(`.gitignore`: "Bundles are build outputs").
"""

import json
import re
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parent.parent
AGENT_DIR = REPO_ROOT / "agent"
SDKCONFIG_DEFAULTS = AGENT_DIR / "sdkconfig.defaults"
WIFI_ADAPTER = AGENT_DIR / "main" / "ff_net_wifi.c"
DIST_DIR = AGENT_DIR / "dist"

# Retyped literally rather than imported, for the same reason test_agent_partitions.py
# retypes its table: a tripwire that imports the constant it guards guards nothing.
REQUIRED_OPTIONS = [
    # S0-fw-3. Applied by esp_clk_init() before app_main, therefore in effect during PHY
    # calibration — which is the whole reason it is a candidate fix and not just a power
    # saving. See the block comment in agent/sdkconfig.defaults.
    "CONFIG_ESP_DEFAULT_CPU_FREQ_MHZ_80=y",
    # The image has to fit an OTA slot it must also be able to download a replacement
    # into. IDF's default for a new project is -Og.
    "CONFIG_COMPILER_OPTIMIZATION_SIZE=y",
]

# Enabled anywhere — including a per-target delta — these silently undo the two above.
# A per-target file is the likely place for it to happen: it is the file someone edits
# when one chip misbehaves, and nothing else would notice the whole fleet's clock moved.
FORBIDDEN_OPTIONS = [
    "CONFIG_ESP_DEFAULT_CPU_FREQ_MHZ_160",
    "CONFIG_ESP_DEFAULT_CPU_FREQ_MHZ_240",
    "CONFIG_COMPILER_OPTIMIZATION_DEBUG",
    "CONFIG_COMPILER_OPTIMIZATION_PERF",
    # -Os is worth having; -Os bought by deleting the assertions is not. The agent's
    # diagnostic output is the product (S0-fe-4 .. S0-fe-7), and an assertion that fires
    # with no message is the exact "infer it from someone else's log" failure the
    # enrollment console exists to remove.
    "CONFIG_COMPILER_OPTIMIZATION_ASSERTIONS_DISABLE",
]

# Measured 2026-09-13, at -Os and with the TX-power retry ladder. The -Og images these
# replaced were 1,079,520 / 1,060,800 / 1,129,856 / 1,181,840 — the optimization level
# alone was worth 8-9% on every target, of which the ladder gave back ~1.3 KB.
#
# PER TARGET, not one fleet-wide number, because the four differ by ~120 KB — the RISC-V
# targets are consistently larger than the Xtensa esp32. A single ceiling set at the
# largest would let esp32 grow 100 KB without a word, which is most of what this test is
# for. Each number is a ceiling, not a target: it exists so the value can only go down.
#
# RATCHET THESE when a build measures smaller. The budget is worth exactly the amount by
# which it is tighter than the last known image, so leaving slack after a win throws the
# win away.
APP_SIZE_BUDGET_BYTES = {
    "esp32": 991_776,
    "esp32s3": 971_168,
    "esp32c3": 1_026_240,
    "esp32c6": 1_075_744,
}

# An app may occupy at most this much of an OTA slot. Not a style rule: R2 downloads the
# next image into the *other* slot while running from this one, and R5 adds signature
# verification on top. An image at 90% of its slot is an image that cannot be replaced.
MAX_SLOT_FRACTION = 0.70


def _config_lines(path: Path) -> list[str]:
    """Every non-comment, non-blank line of an sdkconfig fragment."""
    return [
        line.strip()
        for line in path.read_text().splitlines()
        if line.strip() and not line.lstrip().startswith("#")
    ]


def _enabled_options(path: Path) -> list[str]:
    """The names of every `CONFIG_X=y` in `path`."""
    return [
        line.split("=", 1)[0]
        for line in _config_lines(path)
        if line.startswith("CONFIG_") and line.split("=", 1)[1].strip() == "y"
    ]


def _all_sdkconfig_files() -> list[Path]:
    return [SDKCONFIG_DEFAULTS, *sorted(AGENT_DIR.glob("sdkconfig.defaults.*"))]


def _bundled_manifests() -> list[Path]:
    return sorted(DIST_DIR.glob("*/manifest.json"))


class TestPowerPosture:
    """The decisions that change what a board draws, none of which show up in behaviour."""

    def test_cpu_runs_at_80mhz(self) -> None:
        """160 MHz buys an I/O-bound agent nothing and costs ~20-30 mA continuously."""
        assert "CONFIG_ESP_DEFAULT_CPU_FREQ_MHZ_80=y" in _config_lines(SDKCONFIG_DEFAULTS)

    def test_reduced_tx_power_after_brownout_is_still_set(self) -> None:
        """S0-fw-3's v0.3.3 change, kept after it failed to clear the fault.

        It did not break the loop on the one board in the loop, but it is correct and
        inert on a healthy board, and it is what makes the escape *visible* when one
        happens. Deleting it because "it didn't work" would also delete the reporting.
        """
        assert "CONFIG_ESP_PHY_REDUCE_TX_POWER=y" in _config_lines(SDKCONFIG_DEFAULTS)

    def test_fleet_wide_tx_power_is_not_reduced(self) -> None:
        """The targeted fix must not quietly become a fleet-wide one.

        `CONFIG_ESP_PHY_MAX_WIFI_TX_POWER` stays at the 20 dBm default: trading range on
        every board for a fault only some have is a different decision from the one that
        was made, and it would show up as unexplained dropouts, not as an error.
        """
        for path in _all_sdkconfig_files():
            for line in _config_lines(path):
                assert not line.startswith("CONFIG_ESP_PHY_MAX_WIFI_TX_POWER="), (
                    f"{path.name} sets fleet-wide TX power; S0-fw-3 deliberately did not"
                )

    def test_the_reconnect_ladder_varies_tx_power(self) -> None:
        """Retrying forever is only useful if the attempts differ.

        The runtime API, not the compile-time one: `esp_wifi_set_max_tx_power` backs off
        a single board that has proven it cannot associate at full power, where
        `CONFIG_ESP_PHY_MAX_WIFI_TX_POWER` would cost every board in the fleet range to
        help the few with bad supplies. S0-fw-3 refused the second; this is the first.
        """
        source = WIFI_ADAPTER.read_text()
        assert "esp_wifi_set_max_tx_power" in source, (
            "the Wi-Fi reconnect must vary TX power across attempts, not repeat one attempt"
        )
        assert "esp_wifi_get_max_tx_power" in source, (
            "rung 0 must be READ from the driver, not hardcoded — a literal 20 dBm would "
            "silently override CONFIG_ESP_PHY_REDUCE_TX_POWER on a post-brownout boot"
        )

    def test_the_reconnect_never_gives_up(self) -> None:
        """The one outcome that costs a truck roll.

        `agent_main.c`'s header and this adapter's both state it: a board that stops
        trying needs a site visit, which is the intervention this product exists to
        remove. An AP that reboots, a DHCP server that is slow, a site whose uplink is
        out for a day — all of them are survivable only by a board that is still trying
        when they come back. Bounding the retries would convert every one of them into a
        dispatch.

        Checked as a property of the loop's shape: the backoff is capped (so a failing
        board does not hammer the AP) and nothing counts attempts toward a give-up.
        """
        source = WIFI_ADAPTER.read_text()
        assert "WIFI_RETRY_MAX_MS" in source, "the backoff must stay capped"
        for giving_up in ("MAX_RETRIES", "MAX_ATTEMPTS", "RETRY_LIMIT", "GIVE_UP"):
            assert giving_up not in source, (
                f"{giving_up} suggests a bounded retry; a board that stops trying to "
                "reach its network is a site visit"
            )

    def test_modem_sleep_is_requested_explicitly(self) -> None:
        """IDF's default already sleeps. That is not the same as having decided to.

        A default can change with an IDF pin bump — and this one is load-bearing for a
        product whose boards may end up somewhere without mains. QEMU has no Wi-Fi, so
        the radio's actual behaviour is a bench measurement (S0-test-1); what is
        checkable here is that the call is present and says which mode it wants.
        """
        source = WIFI_ADAPTER.read_text()
        assert re.search(r"esp_wifi_set_ps\s*\(\s*WIFI_PS_MAX_MODEM\s*\)", source), (
            "ff_net_wifi.c must set its power-save mode explicitly, not inherit it"
        )


class TestFlashBudget:
    """`-Os`, and a ceiling on what reaches an OTA slot."""

    @pytest.mark.parametrize("option", REQUIRED_OPTIONS)
    def test_required_option_is_present(self, option: str) -> None:
        assert option in _config_lines(SDKCONFIG_DEFAULTS)

    @pytest.mark.parametrize("option", FORBIDDEN_OPTIONS)
    def test_forbidden_option_is_not_enabled_anywhere(self, option: str) -> None:
        for path in _all_sdkconfig_files():
            assert option not in _enabled_options(path), (
                f"{path.name} enables {option}, undoing a decision made in "
                f"{SDKCONFIG_DEFAULTS.name}"
            )

    def test_assertions_survive_the_size_work(self) -> None:
        """-Os is independent of assertions; make sure nobody "optimises" by deleting them."""
        for path in _all_sdkconfig_files():
            for line in _config_lines(path):
                assert line != "CONFIG_COMPILER_OPTIMIZATION_ASSERTION_LEVEL=0", (
                    f"{path.name} silences assertions — the agent's diagnostics are the product"
                )

    def test_every_built_bundle_is_within_budget(self) -> None:
        """Skipped on a clean clone: `agent/dist/*` is a build output, not a source."""
        manifests = _bundled_manifests()
        if not manifests:
            pytest.skip("no bundle in agent/dist — run `just agent-build-all` to check sizes")

        oversized = []
        for path in manifests:
            manifest = json.loads(path.read_text())
            target = manifest["target"]
            app = next(part for part in manifest["parts"] if part["name"] == "app")
            budget = APP_SIZE_BUDGET_BYTES.get(target)
            # An unbudgeted target is a new target. Fail rather than skip: the point of
            # the table is that every shipped image has a recorded ceiling.
            assert budget is not None, (
                f"{target} has no entry in APP_SIZE_BUDGET_BYTES — add one when adding a target"
            )
            if app["size"] > budget:
                oversized.append(f"{target}: {app['size']} > {budget}")
        assert not oversized, (
            f"over budget: {', '.join(oversized)}. An image only ever gets bigger by "
            "accident; raise the budget deliberately or find the bytes."
        )

    def test_every_built_bundle_leaves_room_to_be_replaced(self) -> None:
        """The failure this prevents is a fleet that cannot be updated out of trouble."""
        manifests = _bundled_manifests()
        if not manifests:
            pytest.skip("no bundle in agent/dist — run `just agent-build-all` to check sizes")

        for path in manifests:
            manifest = json.loads(path.read_text())
            app = next(part for part in manifest["parts"] if part["name"] == "app")
            slot = manifest["ota_slot_size"]
            fraction = app["size"] / slot
            assert fraction <= MAX_SLOT_FRACTION, (
                f"{manifest['target']}: app.bin is {fraction:.0%} of its {slot}-byte OTA "
                f"slot (limit {MAX_SLOT_FRACTION:.0%})"
            )

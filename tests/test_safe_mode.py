"""Rapid-crash safe mode (R5-fw-6, R5-spec-1 D13).

Three `panic`/`int_wdt`/`task_wdt`/`wdt` resets within 60 s on the same confirmed image hold
the board in safe mode: only the agent runs, the firmware's own code does not, and the
announce says `"safe_mode": true`. It leaves on a successful update (another slot), a
`reboot` command (a software restart) or a power cycle (RTC memory is lost). No host test can
run the firmware; the QEMU run is the proof (DECISIONS.md, R5-fw-6). What is held here, on
every `just test`, are the shapes that make it true:

* the header: one PUBLIC, non-maker function, called by all three boot paths; the announce's
  read is private;
* the boot paths: the check is the ff call right after the confirm-timer arm (before the
  twice-called test); the two ladders that run firmware code hold after `s_started = true`
  and before `return true`, guarded by the check, in a `noreturn` loop that never reboots,
  aborts or reports a stage; the stock agent never holds;
* the module: an RTC_NOINIT record with a magic and a crc, exactly the four counted reasons
  (never `brownout`), power-on clears, the slot is compared, only VALID/UNDEFINED (or no
  otadata entry) is confirmed, the constants 3 and 60, the idempotence guard, no NVS, no
  esp_timer, no reboot; the breadcrumb is a self-deleting task that never logs;
* the announce: `safe_mode` only as `true`, only when active, between `reset_reason` and
  `lib_marker`, and its boot line before the `device_id` line;
* no verb path knows about safe mode, and the spec's announce example has no `safe_mode` key.

Comments are stripped before any grep (`_code`), the idiom of the other agent tripwires.
"""

import json
import re

from tests.agent_src import (
    AGENT_MAIN_C,
    ARDUINO_WRAPPER_CPP,
    ARDUINO_WRAPPER_H,
    COMPONENT_CMAKE,
    COMPONENT_INCLUDE,
    COMPONENT_SRC,
    FF_SAFE_MODE_C,
    FF_SAFE_MODE_H,
    FF_SAFE_MODE_INTERNAL_H,
    IDF_EXAMPLE_MAIN_C,
    IDF_EXAMPLE_START_C,
)
from tests.test_agent_txn import FF_MQTT_C, FF_OTA_C, _function_body
from tests.test_arduino_library import _begin_body, _ff_calls
from tests.test_ff_cfg import _code
from tests.test_library_marker import DEVICE_PROTOCOL
from tests.test_ota_component import MAKER_HEADERS, _declared_functions

FF_IDENTITY_C = COMPONENT_SRC / "ff_identity.c"
FF_IDENTITY_INTERNAL_H = COMPONENT_SRC / "ff_identity_internal.h"

ARM = "ff_mqtt_arm_confirm_timer"
CHECK = "ff_safe_mode_check"
COUNTED = {"ESP_RST_PANIC", "ESP_RST_INT_WDT", "ESP_RST_TASK_WDT", "ESP_RST_WDT"}


def _src() -> str:
    return _code(FF_SAFE_MODE_C)


def _start_body() -> str:
    return _function_body(_code(IDF_EXAMPLE_START_C), "fleetforge_start")


def _ladders() -> dict[str, tuple[str, str]]:
    """(the boot function's body, the whole file) for each ladder that runs firmware code."""
    return {
        "Fleetforge.cpp": (_begin_body(), _code(ARDUINO_WRAPPER_CPP)),
        "fleetforge_start.c": (_start_body(), _code(IDF_EXAMPLE_START_C)),
    }


class TestTheHeader:
    def test_one_public_function(self) -> None:
        assert FF_SAFE_MODE_H.parent == COMPONENT_INCLUDE
        assert _declared_functions(FF_SAFE_MODE_H) == {CHECK}
        assert "bool ff_safe_mode_check(void);" in _code(FF_SAFE_MODE_H)

    def test_it_is_not_a_maker_header(self) -> None:
        """All three boot paths call it, so it is agent surface (agent_main.c includes it)."""
        assert FF_SAFE_MODE_H.name not in MAKER_HEADERS
        assert '#include "ff_safe_mode.h"' in AGENT_MAIN_C.read_text()

    def test_extern_c_and_minimal_includes(self) -> None:
        header = _code(FF_SAFE_MODE_H)
        assert 'extern "C" {' in header
        assert re.findall(r"^#include (\S+)", header, re.M) == ["<stdbool.h>"]

    def test_the_contract_is_written_down(self) -> None:
        text = FF_SAFE_MODE_H.read_text()
        for phrase in (
            "ADDITIVE-ONLY",
            "right after",
            "ff_mqtt_arm_confirm_timer()",
            "never return",
            "no NVS",
            "Idempotent",
            "brownout",
        ):
            assert phrase in text, phrase

    def test_the_announce_read_is_private(self) -> None:
        assert FF_SAFE_MODE_INTERNAL_H.parent == COMPONENT_SRC
        assert _declared_functions(FF_SAFE_MODE_INTERNAL_H) == {"ff_safe_mode_active"}
        for header in sorted(COMPONENT_INCLUDE.glob("*.h")):
            assert "ff_safe_mode_active" not in _code(header), header.name
        for path in (ARDUINO_WRAPPER_CPP, ARDUINO_WRAPPER_H, AGENT_MAIN_C, IDF_EXAMPLE_START_C):
            assert "ff_safe_mode_active" not in _code(path), path.name

    def test_the_component_compiles_it(self) -> None:
        assert '"src/ff_safe_mode.c"' in COMPONENT_CMAKE.read_text()
        assert 'static const char *TAG = "ff-safemode";' in _src()


class TestTheBootPaths:
    def test_agent_main_checks_second_and_never_holds(self) -> None:
        body = _function_body(_code(AGENT_MAIN_C), "app_main")
        calls = _ff_calls(body)
        assert calls[:2] == [ARM, CHECK], calls
        assert calls.count(CHECK) == 1
        between = body[body.index(f"{ARM}();") + len(f"{ARM}();") : body.index(CHECK)]
        assert between.strip() == "(void)", between
        assert "hold_in_safe_mode" not in _code(AGENT_MAIN_C)

    def test_each_ladder_checks_right_after_the_arm_before_the_twice_test(self) -> None:
        for name, (body, _whole) in _ladders().items():
            calls = _ff_calls(body)
            assert calls[:2] == [ARM, CHECK], (name, calls)
            assert calls.count(CHECK) == 1, name
            check = body.index(f"const bool safe_mode = {CHECK}();")
            assert body.index(f"{ARM}();") < check < body.index("if (s_started)"), name

    def test_each_ladder_holds_after_started_and_before_returning_true(self) -> None:
        for name, (body, _whole) in _ladders().items():
            hold = re.search(r"if \(safe_mode\) \{\s*hold_in_safe_mode\(\);\s*\}", body)
            assert hold is not None, name
            assert body.count("hold_in_safe_mode()") == 1, name
            assert body.index("s_started = true;") < hold.start(), name
            tail = body[hold.end() :]
            assert tail.strip() == "return true;", (name, tail)

    def test_each_ladder_defines_a_noreturn_hold_that_never_leaves(self) -> None:
        for name, (_body, whole) in _ladders().items():
            assert "static void hold_in_safe_mode(void) __attribute__((noreturn));" in whole, name
            hold = _function_body(whole, "hold_in_safe_mode")
            assert "while (true)" in hold, name
            assert "vTaskDelay(pdMS_TO_TICKS(300000));" in hold, name
            assert "ESP_LOGW(TAG, " in hold, name
            for forbidden in ("return", "esp_restart", "abort", "ff_progress_report", "park("):
                assert not re.search(rf"\b{re.escape(forbidden)}", hold), (name, forbidden)

    def test_the_hold_line_says_what_is_held_and_how_to_leave(self) -> None:
        for name, (_body, whole) in _ladders().items():
            hold = _function_body(whole, "hold_in_safe_mode")
            assert "safe mode:" in hold, name
            assert "an update, a reboot command or a power cycle" in hold, name
        assert "setup() and loop() are not running" in _code(ARDUINO_WRAPPER_CPP)
        assert "your app_main is not running past fleetforge_start()" in _code(IDF_EXAMPLE_START_C)

    def test_the_wrapper_raises_the_new_tag(self) -> None:
        listed = re.search(r"FF_LOG_TAGS\[\] = \{([^}]*)\}", _code(ARDUINO_WRAPPER_CPP))
        assert listed is not None
        assert '"ff-safemode"' in listed.group(1)

    def test_the_idf_main_is_unchanged(self) -> None:
        """The hold lives in fleetforge_start(); the example's own main.c knows nothing."""
        assert "safe_mode" not in _code(IDF_EXAMPLE_MAIN_C)


class TestTheModule:
    def test_the_record_is_rtc_noinit_with_a_magic_and_a_crc(self) -> None:
        src = _src()
        assert "static RTC_NOINIT_ATTR ff_safe_mode_rtc_t s_rtc;" in src
        assert re.search(r"#define FF_SAFE_MODE_MAGIC 0x[0-9A-Fa-f]+u", src)
        assert (
            "esp_rom_crc32_le(0, (const uint8_t *)&s_rtc, offsetof(ff_safe_mode_rtc_t, crc))" in src
        )
        valid = _function_body(src, "record_is_valid")
        assert "s_rtc.magic == FF_SAFE_MODE_MAGIC" in valid and "s_rtc.crc == record_crc()" in valid

    def test_every_write_is_sealed(self) -> None:
        """A field written without a fresh crc reads as garbage at the next boot."""
        src = _src()
        check = _function_body(src, "ff_safe_mode_check")
        tail = check[check.index("s_rtc.slot = slot;") :]
        assert "start_breadcrumb();" in tail and "seal();" in tail
        crumb = _function_body(src, "start_breadcrumb")
        assert crumb.count("seal();") == 2
        task = _function_body(src, "alive_task")
        assert task.index("s_rtc.alive_s =") < task.index("seal();")

    def test_the_counted_set_is_exactly_the_spec_four(self) -> None:
        body = _function_body(_src(), "is_counted")
        cases = set(re.findall(r"case (ESP_RST_[A-Z_]+):", body))
        assert cases == COUNTED
        assert re.search(r"default:\s*return false;", body)
        assert "ESP_RST_BROWNOUT" not in body
        assert "ESP_RST_CPU_LOCKUP" not in body

    def test_power_on_an_invalid_record_a_new_slot_or_an_unconfirmed_image_start_afresh(
        self,
    ) -> None:
        check = _function_body(_src(), "ff_safe_mode_check")
        first = re.search(r"if \(([^{]*)\) \{\s*fresh_record\(\);", check)
        assert first is not None
        condition = first.group(1)
        for term in (
            "!valid",
            "reason == ESP_RST_POWERON",
            "slot_changed",
            "!image_is_confirmed(running)",
        ):
            assert term in condition, term
        assert "s_rtc.slot != slot" in check
        assert "running->address" in check

    def test_only_valid_undefined_or_usb_flashed_is_confirmed(self) -> None:
        body = _function_body(_src(), "image_is_confirmed")
        assert "if (running == NULL) {\n        return false;" in body
        assert re.search(r"!= ESP_OK\) \{\s*return true;", body)
        assert "return state == ESP_OTA_IMG_VALID || state == ESP_OTA_IMG_UNDEFINED;" in body
        for state in ("ESP_OTA_IMG_PENDING_VERIFY", "ESP_OTA_IMG_NEW"):
            assert state not in body, state

    def test_a_counted_crash_shifts_the_lifetimes_and_latches_inside_the_window(self) -> None:
        check = _function_body(_src(), "ff_safe_mode_check")
        branch = check[
            check.index("} else if (is_counted(reason)) {") : check.index(
                "} else if (reason == ESP_RST_BROWNOUT) {"
            )
        ]
        assert "s_rtc.life_s[FF_SAFE_MODE_LIVES - 1u] = s_rtc.alive_s;" in branch
        assert "if (s_rtc.crashes < UINT8_MAX)" in branch
        assert "s_rtc.crashes >= FF_SAFE_MODE_CRASHES" in branch
        assert "window_s <= FF_SAFE_MODE_WINDOW_S" in branch
        assert "s_rtc.latched = 1;" in branch
        assert "s_rtc.latched = 0" not in check

    def test_a_brownout_clears_the_count_and_keeps_the_latch(self) -> None:
        check = _function_body(_src(), "ff_safe_mode_check")
        start = check.index("} else if (reason == ESP_RST_BROWNOUT) {")
        branch = check[start : check.index("} else {", start)]
        assert "s_rtc.crashes = 0;" in branch
        assert "memset(s_rtc.life_s, 0, sizeof s_rtc.life_s);" in branch
        assert "latched" not in branch and "fresh_record" not in branch
        # Every other reason (sw, ext, usb, jtag, deepsleep, ...) leaves: the final else.
        assert re.search(r"\} else \{\s*fresh_record\(\);\s*\}", check[start:])

    def test_the_constants_are_three_and_sixty_and_asserted(self) -> None:
        src = _src()
        assert "#define FF_SAFE_MODE_CRASHES 3u" in src
        assert "#define FF_SAFE_MODE_WINDOW_S 60u" in src
        assert src.count("_Static_assert(FF_SAFE_MODE_CRASHES") == 1
        assert src.count("_Static_assert(FF_SAFE_MODE_WINDOW_S") == 1

    def test_it_is_idempotent(self) -> None:
        check = _function_body(_src(), "ff_safe_mode_check")
        assert check.strip().startswith("if (s_checked) {\n        return s_latched;\n    }")
        assert check.index("s_checked = true;") < check.index("esp_reset_reason()")
        assert _function_body(_src(), "ff_safe_mode_active").strip() == (
            "return s_checked && s_latched;"
        )

    def test_no_nvs_no_esp_timer_no_reboot(self) -> None:
        src = _src()
        for forbidden in ("nvs_", "ff_store_", "ff_txn_", "esp_timer", "esp_restart", "abort"):
            assert forbidden not in src, forbidden
        includes = re.findall(r'^#include "([^"]+)"', src, re.M)
        assert "esp_timer.h" not in includes and "nvs.h" not in includes

    def test_the_breadcrumb_is_a_quiet_self_deleting_task(self) -> None:
        src = _src()
        task = _function_body(src, "alive_task")
        assert "vTaskDelay(pdMS_TO_TICKS(1000));" in task
        assert task.strip().endswith("vTaskDelete(NULL);")
        assert "ESP_LOG" not in task
        assert "elapsed < FF_SAFE_MODE_WINDOW_S" in task
        crumb = _function_body(src, "start_breadcrumb")
        assert 'xTaskCreate(alive_task, "ff_alive",' in crumb
        # A task that cannot be created means "unknown": the whole window, never counting.
        assert "s_rtc.alive_s = FF_SAFE_MODE_WINDOW_S;" in crumb

    def test_the_breadcrumb_runs_only_on_a_counted_unlatched_boot(self) -> None:
        check = _function_body(_src(), "ff_safe_mode_check")
        assert re.search(
            r"if \(counted && !s_latched\) \{\s*start_breadcrumb\(\);\s*\} else \{\s*"
            r"s_rtc\.alive_s = FF_SAFE_MODE_WINDOW_S;",
            check,
        )
        assert check.count("start_breadcrumb()") == 1

    def test_the_reason_names_are_ff_identitys(self) -> None:
        """One vocabulary: the announce's `reset_reason` names, through a wrapper."""
        assert "ff_identity_reset_reason_name(reason)" in _src()
        assert "const char *ff_identity_reset_reason_name(esp_reset_reason_t reason);" in _code(
            FF_IDENTITY_INTERNAL_H
        )
        wrapper = _function_body(_code(FF_IDENTITY_C), "ff_identity_reset_reason_name")
        assert wrapper.strip() == "return reset_reason_name(reason);"

    def test_the_log_lines(self) -> None:
        src = _src()
        for phrase in (
            '"SAFE MODE: three crash resets within %u s on this confirmed image "',
            '"still in SAFE MODE (last reset: %s): only the fleetforge agent runs"',
            '"the last boot ended in %s after <= %u s: crash reset %u of %u on "',
            '"safe mode left: another image is running"',
            '"safe mode left: a software restart (a reboot command, an update or "',
            '"safe mode left: reset by %s"',
        ):
            assert phrase in src, phrase
        assert "%f" not in src


class TestTheAnnounce:
    def test_safe_mode_is_true_only_and_only_when_active(self) -> None:
        body = _function_body(_code(FF_IDENTITY_C), "announce_object")
        assert re.search(
            r'if \(ff_safe_mode_active\(\)\) \{\s*cJSON_AddTrueToObject\(root, "safe_mode"\);\s*\}',
            body,
        )
        assert body.count('"safe_mode"') == 1
        code = _code(FF_IDENTITY_C)
        assert not re.search(r'cJSON_Add(False|Bool)ToObject\([^;]*"safe_mode"', code)

    def test_between_reset_reason_and_lib_marker(self) -> None:
        body = _function_body(_code(FF_IDENTITY_C), "announce_object")
        reset = body.index('"reset_reason", s_reset_reason')
        safe = body.index('"safe_mode"')
        assert reset < safe < body.index('"lib_marker"')

    def test_the_boot_line_precedes_the_device_id_line(self) -> None:
        """frontend/src/diagnostics.ts reads the id from the last ff-id line."""
        init = _function_body(_code(FF_IDENTITY_C), "ff_identity_init")
        line = re.search(
            r'if \(ff_safe_mode_active\(\)\) \{\s*ESP_LOGI\(TAG, "safe_mode true \(announced\)"\);',
            init,
        )
        assert line is not None
        assert init.index('"reset_reason %s (announced)"') < line.start()
        assert line.start() < init.index('"device_id %s"')

    def test_not_in_the_heartbeat(self) -> None:
        body = _function_body(_code(FF_IDENTITY_C), "ff_identity_heartbeat_json")
        assert "safe_mode" not in body

    def test_the_spec_example_has_no_safe_mode_key(self) -> None:
        """R5-spec-1: prose only. In the example, TestAnnounceMatchesTheSpec would make it
        unconditional. Read, never edited."""
        text = DEVICE_PROTOCOL.read_text()
        start = text.index("### `up/announce` — identity")
        block_start = text.index("```json", start) + len("```json")
        example = json.loads(text[block_start : text.index("```", block_start)])
        assert "safe_mode" not in example


class TestNoVerbKnowsAboutIt:
    def test_mqtt_and_ota_never_mention_safe_mode(self) -> None:
        """The exits are reset-reason based (sw, another slot), so the verbs behave
        identically in safe mode and the confirm/rollback path is untouched."""
        for path in (FF_MQTT_C, FF_OTA_C):
            code = _code(path)
            assert "safe_mode" not in code, path.name
            assert "ff_safe_mode" not in code, path.name

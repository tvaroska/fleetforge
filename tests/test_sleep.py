"""The sleep helpers (R5-fw-7): a declared power class, isConnected() and isReadyForSleep().

`ff_set_power_class()` / `Fleetforge.setPowerClass()` declare the image's power class, which the
announce carries instead of ff_cfg's once the session froze it. `ff_is_connected()` says the
fleet acknowledged the announce on the current connection; `ff_is_ready_for_sleep()` says a deep
sleep now loses nothing (R5-spec-1 D14 plus two additions: this wake checked in, telemetry
drained). No host test can run the firmware; the QEMU run is the proof (DECISIONS.md, R5-fw-7).
What is held here, on every `just test`, are the shapes that make it true:

* the maker header: three functions, the enum, `extern "C"`, no private hook public;
* the registration: validation, refusals, decided under the spinlock, one log per refusal;
* the freeze: once, in `ff_mqtt_run`, before the transaction and the first connect;
* the announce: the override is asked before the `power_class` key, and answers only frozen;
* the per-connection and per-boot flags: cleared and set where the ack is, outside `s_ctx`, the
  confirm block unchanged;
* readiness: every rule, in order, the session first; no NVS, no `ff_ota_txn_phase()`, no log,
  never from timer context;
* the Arduino members and both examples.

Comments are stripped before any grep (`_code`), the idiom of the other agent tripwires.
"""

import re

from tests.agent_src import (
    AGENT_MAIN_C,
    ARDUINO_WRAPPER_CPP,
    ARDUINO_WRAPPER_H,
    COMPONENT_CMAKE,
    COMPONENT_INCLUDE,
    COMPONENT_SRC,
    FF_SLEEP_C,
    FF_SLEEP_H,
    FF_SLEEP_INTERNAL_H,
    FF_TELEMETRY_C,
    IDF_EXAMPLE_MAIN_C,
)
from tests.test_agent_txn import FF_MQTT_C, FF_OTA_C, _function_body
from tests.test_ff_cfg import _code
from tests.test_ota_component import MAKER_HEADERS, _declared_functions
from tests.test_selftest import TIMER_CONTEXT, _member_body, _published

FF_IDENTITY_C = COMPONENT_SRC / "ff_identity.c"
FF_OTA_H = COMPONENT_SRC / "ff_ota.h"

PUBLIC_FUNCTIONS = {"ff_set_power_class", "ff_is_connected", "ff_is_ready_for_sleep"}
PRIVATE_HOOKS = (
    "ff_sleep_freeze",
    "ff_sleep_power_override",
    "ff_mqtt_sleep_blocker",
    "ff_mqtt_is_connected",
    "ff_ota_in_flight",
    "ff_telemetry_drained",
)
# Called on every loop pass: none of them may log.
SILENT = {
    FF_SLEEP_C: ("ff_sleep_power_override", "ff_is_connected", "ff_is_ready_for_sleep",
                 "sleep_blocker", "running_image_confirmed"),
    FF_MQTT_C: ("ff_mqtt_sleep_blocker", "ff_mqtt_is_connected"),
    FF_OTA_C: ("ff_ota_in_flight",),
    FF_TELEMETRY_C: ("ff_telemetry_drained",),
}  # fmt: skip
# The readiness path reads; it never writes flash, never loads the transaction record (which
# may erase a torn one), never queues anything and never wakes a task.
READINESS_FORBIDDEN = (
    "ff_ota_txn_phase(",
    "ff_txn_",
    "nvs_",
    "enqueue_",
    "esp_timer_",
    "xTaskNotify",
    "ff_mqtt_publish_status",
    "esp_mqtt_client_publish",
    "esp_mqtt_client_enqueue",
    "esp_restart",
    "esp_ota_mark_app",
    "esp_ota_set_boot_partition",
)


def _src() -> str:
    return _code(FF_SLEEP_C)


def _mqtt() -> str:
    return _code(FF_MQTT_C)


def _readiness_bodies() -> dict[str, str]:
    bodies = {name: _function_body(_src(), name) for name in SILENT[FF_SLEEP_C]}
    for path in (FF_MQTT_C, FF_OTA_C, FF_TELEMETRY_C):
        for name in SILENT[path]:
            bodies[name] = _function_body(_code(path), name)
    return bodies


class TestTheMakerHeader:
    def test_three_functions_and_the_enum(self) -> None:
        assert _declared_functions(FF_SLEEP_H) == PUBLIC_FUNCTIONS
        header = _code(FF_SLEEP_H)
        for prototype in (
            "esp_err_t ff_set_power_class(ff_power_class_t power_class, uint32_t wake_s);",
            "bool ff_is_connected(void);",
            "bool ff_is_ready_for_sleep(void);",
        ):
            assert prototype in header, prototype
        assert re.search(
            r"FF_POWER_ALWAYS_ON = 0,\s*FF_POWER_SLEEPY = 1,\s*\} ff_power_class_t;", header
        )

    def test_extern_c_and_minimal_includes(self) -> None:
        header = _code(FF_SLEEP_H)
        assert 'extern "C" {' in header
        assert re.findall(r"^#include (\S+)", header, re.M) == [
            "<stdbool.h>",
            "<stdint.h>",
            '"esp_err.h"',
        ]

    def test_it_is_a_maker_header(self) -> None:
        assert "ff_sleep.h" in MAKER_HEADERS
        assert FF_SLEEP_H.parent == COMPONENT_INCLUDE
        assert "ff_sleep" not in _code(AGENT_MAIN_C), "the stock agent never sleeps"

    def test_the_contract_is_written_down(self) -> None:
        text = FF_SLEEP_H.read_text()
        for phrase in (
            "ADDITIVE-ONLY",
            "ESP_ERR_INVALID_ARG",
            "ESP_ERR_INVALID_STATE",
            "2147483647",
            "a deep-sleep wake is a reset",
            "rolls back an image",
            "never from an ISR and never from the esp_timer task",
            "Neither ever logs",
            "up to 5 s",
            "Your own wake limit",
            "300 s",
        ):
            assert phrase.lower() in text.lower(), phrase

    def test_the_hooks_are_private(self) -> None:
        assert FF_SLEEP_INTERNAL_H.parent == COMPONENT_SRC
        assert _declared_functions(FF_SLEEP_INTERNAL_H) == {
            "ff_sleep_freeze",
            "ff_sleep_power_override",
        }
        for header in sorted(COMPONENT_INCLUDE.glob("*.h")):
            for hook in PRIVATE_HOOKS:
                assert hook not in _code(header), (header.name, hook)
        for public in (ARDUINO_WRAPPER_H, ARDUINO_WRAPPER_CPP):
            for hook in PRIVATE_HOOKS:
                assert hook not in _code(public), (public.name, hook)

    def test_the_component_compiles_it(self) -> None:
        cmake = COMPONENT_CMAKE.read_text()
        assert '"src/ff_selftest.c"\n                            "src/ff_sleep.c"' in cmake
        assert 'static const char *TAG = "ff-sleep";' in _src()

    def test_no_arduino_sleep_timer_or_nvs_here(self) -> None:
        source = _src()
        for spelling in ("ARDUINO", "esp_timer", "nvs_", "esp_restart", "esp_deep_sleep", "ff_txn"):
            assert spelling not in source, spelling


class TestTheRegistration:
    def _set(self) -> str:
        return _function_body(_src(), "ff_set_power_class")

    def test_validation_comes_before_the_lock(self) -> None:
        body = self._set()
        lock = body.index("portENTER_CRITICAL(&s_lock)")
        assert body.index("wake_s < 1 || wake_s > (uint32_t)INT32_MAX") < lock
        assert body.index("wake_s != 0") < lock
        assert body.index('"unknown power class"') < lock
        assert body.index("return ESP_ERR_INVALID_ARG;") < lock
        assert body.count("return ESP_ERR_INVALID_ARG;") == 1

    def test_decided_under_the_lock_and_logged_after_it(self) -> None:
        body = self._set()
        enter = body.index("portENTER_CRITICAL(&s_lock)")
        leave = body.index("portEXIT_CRITICAL(&s_lock)")
        for read in ("frozen = s_frozen;", "taken = s_registered;", "s_registered = true;"):
            assert enter < body.index(read) < leave, read
        after = body[leave:]
        assert after.count("return ESP_ERR_INVALID_STATE;") == 2
        assert "the fleetforge session has started" in after
        assert "already set; one per image" in after
        assert body.rstrip().endswith("return ESP_OK;")

    def test_one_error_log_per_refusal_and_none_on_success(self) -> None:
        body = self._set()
        assert body.count("ESP_LOGE(") == 3
        assert "ESP_LOGI(" not in body and "ESP_LOGW(" not in body
        # Every log is followed by its return, so a success path cannot reach one.
        for match in re.finditer(r"ESP_LOGE\(", body):
            rest = body[match.end() :]
            assert re.match(r".*?\);\s*return ESP_ERR_INVALID_(ARG|STATE);", rest, re.S), rest[:80]

    def test_never_cleared(self) -> None:
        source = _src()
        assert "s_registered = false" not in source
        assert "s_frozen = false" not in source


class TestTheFreeze:
    def test_once_in_ff_mqtt_run_before_the_transaction_and_the_connect(self) -> None:
        run = _function_body(_mqtt(), "ff_mqtt_run")
        assert run.count("ff_sleep_freeze(cfg);") == 1
        at = run.index("ff_sleep_freeze(cfg);")
        assert run.index("ff_identity_note_safe_window(") < at
        for later in ("ff_txn_init(", "classify_txn(", "esp_mqtt_client_start("):
            assert at < run.index(later), later
        assert _mqtt().count("ff_sleep_freeze(") == 1
        assert '#include "ff_sleep_internal.h"' in FF_MQTT_C.read_text()

    def test_it_logs_once_naming_both_values(self) -> None:
        body = _function_body(_src(), "ff_sleep_freeze")
        enter = body.index("portENTER_CRITICAL(&s_lock)")
        leave = body.index("portEXIT_CRITICAL(&s_lock)")
        assert enter < body.index("s_frozen = true;") < leave
        assert enter < body.index("s_logged = true;") < leave
        assert "static bool s_logged;" in body
        assert leave < body.index("if (!log_now)")
        assert "the firmware declares power_class %s (wake %u s): announced instead of " in body
        assert "ff_cfg's %s (wake_s %d)" in body
        assert "no power class declared by the firmware: power_class %s from ff_cfg" in body

    def test_the_spellings_cannot_drift(self) -> None:
        source = _src()
        assert '#define POWER_SLEEPY "sleepy"' in source
        assert "FF_CFG_DEFAULT_POWER" in _function_body(source, "ff_sleep_power_override")
        assert '"always_on"' not in source


class TestTheAnnounce:
    def test_the_override_answers_only_once_frozen_and_declared(self) -> None:
        body = _function_body(_src(), "ff_sleep_power_override")
        enter = body.index("portENTER_CRITICAL(&s_lock)")
        leave = body.index("portEXIT_CRITICAL(&s_lock)")
        assert enter < body.index("active = s_frozen && s_registered;") < leave
        assert leave < body.index("if (!active)") < body.index("*power =")
        assert "*wake_s = sleepy ? (int)declared_wake_s : 0;" in body

    def test_announce_object_asks_before_the_key(self) -> None:
        body = _function_body(_code(FF_IDENTITY_C), "announce_object")
        ask = body.index("ff_sleep_power_override(&power, &wake_s)")
        assert body.index("const char *power = cfg->power;") < ask
        assert body.index("int wake_s = cfg->wake_s;") < ask
        assert ask < body.index('cJSON_AddStringToObject(root, "power_class", power);')
        assert 'cJSON_AddNumberToObject(root, "expected_wake_interval_s", wake_s);' in body
        assert 'cJSON_AddNullToObject(root, "expected_wake_interval_s");' in body
        assert "cfg->power" not in body.replace("const char *power = cfg->power;", "")
        assert "cfg->wake_s" not in body.replace("int wake_s = cfg->wake_s;", "")

    def test_no_new_capability_or_key(self) -> None:
        body = _function_body(_code(FF_IDENTITY_C), "announce_object")
        assert "sleep" not in re.sub(r"ff_sleep_power_override", "", body)


class TestTheConnectedFlags:
    def test_file_statics_outside_s_ctx(self) -> None:
        code = _mqtt()
        assert "static volatile bool s_acked_this_connection;" in code
        assert "static volatile bool s_checked_in;" in code
        struct = re.search(r"typedef struct \{(.*?)\} ff_mqtt_ctx_t;", code, re.S)
        assert struct is not None
        assert "acked" not in struct.group(1) and "checked_in" not in struct.group(1)

    def test_cleared_at_connect_before_the_announce_and_at_disconnect(self) -> None:
        connected = _function_body(_mqtt(), "on_connected")
        clear = connected.index("s_acked_this_connection = false;")
        assert clear < connected.index("esp_mqtt_client_subscribe(")
        assert clear < connected.index("esp_mqtt_client_publish(")
        down = re.search(r"case MQTT_EVENT_DISCONNECTED:(.*?)\bcase\b", _mqtt(), re.S)
        assert down is not None
        assert "s_acked_this_connection = false;" in down.group(1)

    def test_set_only_at_the_announce_ack(self) -> None:
        code = _mqtt()
        published = _published()
        flags = re.search(
            r"if \(event->msg_id == ctx->announce_msg_id\) \{\s*"
            r"s_acked_this_connection = true;\s*s_checked_in = true;\s*\}",
            published,
        )
        assert flags is not None
        assert code.count("s_acked_this_connection = true;") == 1
        assert code.count("s_checked_in = true;") == 1
        assert "s_checked_in = false" not in code, "a wake checks in once; never un-checked"
        assert "s_checked_in" not in _function_body(code, "on_connected")

    def test_the_confirm_block_follows_unchanged(self) -> None:
        published = _published()
        flags = published.index("s_checked_in = true;")
        confirm = published.index(
            "if (event->msg_id == ctx->announce_msg_id && !ctx->session_confirmed) {"
        )
        assert flags < confirm
        assert (
            published.index("else if (confirm_this_image() && s_txn.kind == TXN_CONFIRMING)")
            > confirm
        )

    def test_no_decision_reads_them(self) -> None:
        code = _mqtt()
        for name in (
            "confirm_timeout_cb",
            "rollback_now_cb",
            "rollback_report_task",
            "rollback_gate_task",
            "start_rollback_gate",
            "selftest_done",
            "classify_txn",
            "report_txn_on_connect",
            "on_command",
        ):
            body = _function_body(code, name)
            for flag in ("s_acked_this_connection", "s_checked_in", "ff_mqtt_sleep_blocker"):
                assert flag not in body, (name, flag)

    def test_is_connected_needs_both(self) -> None:
        body = _function_body(_mqtt(), "ff_mqtt_is_connected")
        assert body.strip() == "return s_ctx.connected && s_acked_this_connection;"
        assert _function_body(_src(), "ff_is_connected").strip() == "return ff_mqtt_is_connected();"


class TestReadiness:
    def test_the_session_rules_in_order_and_the_outbox_last(self) -> None:
        body = _function_body(_mqtt(), "ff_mqtt_sleep_blocker")
        order = [
            "if (!s_checked_in)",
            "if (s_rollback_decided || s_rollback_waiting)",
            "if (s_cmd_reboot_pending)",
            "if (s_txn.kind != TXN_NONE)",
            "if (esp_mqtt_client_get_outbox_size(s_ctx.client) > 0)",
        ]
        positions = [body.index(rule) for rule in order]
        assert positions == sorted(positions)
        assert body.count("s_ctx.client") == 1
        assert body.rstrip().endswith("return NULL;")

    def test_the_component_rules_in_order_the_session_first(self) -> None:
        body = _function_body(_src(), "sleep_blocker")
        order = [
            "ff_mqtt_sleep_blocker()",
            "ff_ota_in_flight()",
            "esp_ota_get_boot_partition()",
            "boot != running",
            "!running_image_confirmed()",
            "!ff_telemetry_drained()",
        ]
        positions = [body.index(rule) for rule in order]
        assert positions == sorted(positions)
        assert body.rstrip().endswith("return NULL;")
        assert _function_body(_src(), "ff_is_ready_for_sleep").strip() == (
            "return sleep_blocker() == NULL;"
        )

    def test_the_confirmed_rule(self) -> None:
        body = _function_body(_src(), "running_image_confirmed")
        assert "err == ESP_ERR_NOT_FOUND || err == ESP_ERR_NOT_SUPPORTED" in body
        assert "return state == ESP_OTA_IMG_VALID || state == ESP_OTA_IMG_UNDEFINED;" in body
        not_ok = body.index("if (err != ESP_OK)")
        assert body[not_ok:].split("}", 1)[0].strip().endswith("return false;")

    def test_nothing_on_the_path_logs(self) -> None:
        for name, body in _readiness_bodies().items():
            assert "ESP_LOG" not in body, name

    def test_nothing_on_the_path_writes_loads_or_wakes(self) -> None:
        for name, body in _readiness_bodies().items():
            for forbidden in READINESS_FORBIDDEN:
                assert forbidden not in body, (name, forbidden)

    def test_never_from_timer_context(self) -> None:
        code = _mqtt()
        for name in TIMER_CONTEXT:
            body = _function_body(code, name)
            for helper in ("ff_mqtt_sleep_blocker", "ff_mqtt_is_connected", "ff_is_", "ff_sleep_"):
                assert helper not in body, (name, helper)

    def test_ota_in_flight_reads_s_running_under_the_cancel_lock(self) -> None:
        body = _function_body(_code(FF_OTA_C), "ff_ota_in_flight")
        enter = body.index("portENTER_CRITICAL(&s_cancel_lock)")
        leave = body.index("portEXIT_CRITICAL(&s_cancel_lock)")
        assert enter < body.index("= s_running;") < leave
        assert re.findall(r"\bs_[a-z_]+\b", body) == ["s_cancel_lock", "s_running", "s_cancel_lock"]
        assert "bool ff_ota_in_flight(void);" in _code(FF_OTA_H)

    def test_telemetry_drained_compares_under_the_ring_lock(self) -> None:
        body = _function_body(_code(FF_TELEMETRY_C), "ff_telemetry_drained")
        enter = body.index("portENTER_CRITICAL(&s_lock)")
        leave = body.index("portEXIT_CRITICAL(&s_lock)")
        assert enter < body.index("= s_head == s_unsent;") < leave
        assert "Notify" not in body and "s_task" not in body


class TestTheArduinoMembers:
    def test_the_declarations(self) -> None:
        header = _code(ARDUINO_WRAPPER_H)
        assert "enum PowerClass : uint8_t { ALWAYS_ON = 0, SLEEPY = 1 };" in header
        assert (
            "bool setPowerClass(PowerClass powerClass, uint32_t wakeIntervalSeconds = 0);" in header
        )
        assert "bool isConnected();" in header
        assert "bool isReadyForSleep();" in header
        assert re.findall(r"^#include (\S+)", header, re.M) == ["<stdint.h>"]

    def test_each_member_is_one_call(self) -> None:
        code = _code(ARDUINO_WRAPPER_CPP)
        for signature, call in (
            (
                "bool FleetforgeClass::setPowerClass(PowerClass powerClass, uint32_t wakeIntervalSeconds)",
                "ff_set_power_class",
            ),
            ("bool FleetforgeClass::isConnected()", "ff_is_connected"),
            ("bool FleetforgeClass::isReadyForSleep()", "ff_is_ready_for_sleep"),
        ):
            body = _member_body(code, signature)
            assert re.findall(r"\b(ff_[a-z0-9_]+)\s*\(", body) == [call], signature

    def test_the_switch_maps_both_and_refuses_the_rest(self) -> None:
        body = _member_body(
            _code(ARDUINO_WRAPPER_CPP),
            "bool FleetforgeClass::setPowerClass(PowerClass powerClass, uint32_t wakeIntervalSeconds)",
        )
        assert re.search(r"case ALWAYS_ON:\s*power_class = FF_POWER_ALWAYS_ON;", body)
        assert re.search(r"case SLEEPY:\s*power_class = FF_POWER_SLEEPY;", body)
        assert re.search(r"default:\s*return false;", body)
        assert "== ESP_OK" in body

    def test_the_tag_is_raised(self) -> None:
        assert '"ff-selftest", "ff-sleep",' in _code(ARDUINO_WRAPPER_CPP)


class TestTheIdfExample:
    def test_it_declares_fourth_and_reports_from_the_loop(self) -> None:
        main = _code(IDF_EXAMPLE_MAIN_C)
        app = _function_body(main, "app_main")
        statements = [s.strip() for s in app.split(";")]
        assert statements[3] == "ff_set_power_class(FF_POWER_ALWAYS_ON, 0)"
        loop = app[app.index("for (unsigned loops") :]
        assert "ff_is_connected()" in loop
        assert "ff_is_ready_for_sleep()" in loop
        assert '#include "ff_sleep.h"' in main
        assert len(IDF_EXAMPLE_MAIN_C.read_text().splitlines()) <= 70

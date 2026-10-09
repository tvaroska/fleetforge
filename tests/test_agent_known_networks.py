"""The agent reads a list of known networks and joins the first one it can see (R2b-fw-1).

spec/device-protocol.md -> *Known networks* and *up/announce*. No host test can execute
`agent/components/fleetforge/src/*.c`, and QEMU has no Wi-Fi radio, so the scan/selection loop itself is proven
only on the bench (R2b-test-4). The QEMU run proves the parser (an ethernet board with a
list logs `networks  N in ff_cfg, unused`) and the announce (`"ssid":null,
"known_networks":null` on ethernet). What CAN be held here, on every `just test`, is the
shape that makes the rest true. Each of these fails silently on a fielded board:

* the reader parses `nets` with the type checks the spec's malformed list names, and caps
  it at FF_CFG_MAX_NETS;
* no log line in the reader or the Wi-Fi adapter is handed a passphrase (only its length);
* a scan is started from exactly one function, and that function is never reached from the
  path that has an address — "never scan while associated";
* the single-network path is guarded off from the scan, so an old blob connects directly;
* the console phrases the flasher's classifier keys on are still there, byte for byte;
* scan results are read one record at a time and the driver's list is always freed;
* the announce emits `ssid` and `known_networks` right after `link_type`, through the seam;
* the link adapter is started once, however often agent_main retries the bring-up, and a
  retry cannot throw away an address that arrived between two calls;
* the confirm timer is still the first statement of app_main (CRITICAL).

Same idiom as `test_agent_redelivery.py`: comments are stripped, because they quote the
very spellings these tests forbid.
"""

import re
from pathlib import Path

from tests.agent_src import AGENT_MAIN_C, COMPONENT_SRC
from tests.test_agent_txn import _function_body
from tests.test_ff_cfg import FF_CFG_C, FF_IDENTITY_C, _code

FF_NET_C = COMPONENT_SRC / "ff_net.c"
# ff_net_ssid() is component-private since R3-fw-2: only ff_identity.c reports it.
FF_NET_INTERNAL_H = COMPONENT_SRC / "ff_net_internal.h"
FF_NET_WIFI_C = COMPONENT_SRC / "ff_net_wifi.c"

LOG_CALL = re.compile(r"ESP_LOG[EWIDV]\((.*?)\);", re.DOTALL)


def _defined_functions(source: str) -> list[str]:
    """Names of the static/non-static functions DEFINED in a comment-stripped C file."""
    return re.findall(
        r"^(?:static\s+)?[a-z_][a-z0-9_ \*]*?\b([a-z_][a-z0-9_]*)\s*\([^;{)]*\)\s*\{",
        source,
        re.MULTILINE,
    )


def _callers_of(source: str, callee: str) -> set[str]:
    return {
        name
        for name in _defined_functions(source)
        if name != callee and f"{callee}(" in _function_body(source, name)
    }


class TestTheReader:
    def test_it_reads_nets_with_the_type_checks(self) -> None:
        source = _code(FF_CFG_C)
        assert '"nets"' in source
        body = _function_body(source, "parse_nets")
        assert "cJSON_IsArray" in body
        assert "cJSON_IsObject" in body
        assert "FF_CFG_MAX_NETS" in body
        assert "nets_dropped++" in body, "entries past capacity must be counted, not refused"

    def test_it_is_called_from_the_payload_parser(self) -> None:
        assert "parse_nets(root, out)" in _function_body(_code(FF_CFG_C), "parse_payload")

    def test_the_malformed_and_too_long_lines_are_there(self) -> None:
        source = _code(FF_CFG_C)
        assert "config key 'nets' is not an array" in source
        assert "is malformed — the board idles until it is re-flashed" in source
        assert "this agent keeps the first %d and ignores the" in source

    def test_the_old_single_network_struct_members_are_gone(self) -> None:
        """One source of truth: nets[0] IS the top-level ssid/psk."""
        for path in (FF_CFG_C, FF_NET_WIFI_C, FF_IDENTITY_C):
            source = _code(path)
            assert "cfg->ssid" not in source
            assert "cfg->psk" not in source


class TestNoPassphraseIsLogged:
    def _offending(self, path: Path) -> list[str]:
        bad = []
        for call in LOG_CALL.findall(_code(path)):
            without_lengths = re.sub(r"strlen\([^()]*\)", "", call)
            if "psk" in without_lengths or "password" in without_lengths:
                bad.append(call)
        return bad

    def test_the_reader_logs_only_lengths(self) -> None:
        assert self._offending(FF_CFG_C) == []

    def test_the_wifi_adapter_never_logs_one(self) -> None:
        assert self._offending(FF_NET_WIFI_C) == []

    def test_the_lengths_line_survives(self) -> None:
        assert "passphrase %u chars (never printed)" in _code(FF_CFG_C)


class TestSelection:
    def test_a_scan_is_started_only_from_begin_cycle(self) -> None:
        source = _code(FF_NET_WIFI_C)
        assert source.count("esp_wifi_scan_start(") == 1
        assert "esp_wifi_scan_start(" in _function_body(source, "begin_cycle")

    def test_begin_cycle_is_never_reached_from_the_path_with_an_address(self) -> None:
        source = _code(FF_NET_WIFI_C)
        assert "begin_cycle(" not in _function_body(source, "on_got_ip")
        assert "begin_cycle(" not in _function_body(source, "try_candidate")
        assert "begin_cycle(" not in _function_body(source, "on_dhcp_timeout")
        assert _callers_of(source, "begin_cycle") <= {
            "on_wifi_event",
            "on_scan_done",
            "on_disconnected_multi",
        }

    def test_sta_connected_never_scans(self) -> None:
        handler = _function_body(_code(FF_NET_WIFI_C), "on_wifi_event")
        connected = handler[handler.index("WIFI_EVENT_STA_CONNECTED") :]
        connected = connected[: connected.index("return;")]
        assert "begin_cycle(" not in connected
        assert "esp_wifi_scan_start(" not in connected

    def test_the_single_network_path_is_guarded_off_the_scan(self) -> None:
        handler = _function_body(_code(FF_NET_WIFI_C), "on_wifi_event")
        start = handler[handler.index("WIFI_EVENT_STA_START") :]
        start = start[: start.index("return;")]
        assert "s_net_count > 1" in start
        assert "esp_wifi_connect();" in start, "one network connects directly, no scan"

    def test_scan_results_are_read_one_record_at_a_time_and_freed(self) -> None:
        source = _code(FF_NET_WIFI_C)
        body = _function_body(source, "on_scan_done")
        assert "esp_wifi_scan_get_ap_record(" in body
        assert "esp_wifi_clear_ap_list(" in body
        assert "esp_wifi_scan_get_ap_records(" not in source
        # No early return between the read loop and the free.
        assert "return" not in body[: body.index("esp_wifi_clear_ap_list(")]

    def test_the_candidate_takes_the_strongest_ap_of_that_ssid(self) -> None:
        body = _function_body(_code(FF_NET_WIFI_C), "try_candidate")
        assert "WIFI_ALL_CHANNEL_SCAN" in body
        assert "WIFI_CONNECT_AP_BY_SIGNAL" in body

    def test_the_dhcp_watchdog_never_drops_a_link_with_an_address(self) -> None:
        source = _code(FF_NET_WIFI_C)
        body = _function_body(source, "on_dhcp_timeout")
        assert "s_have_ip" in body
        assert body.index("s_have_ip") < body.index("esp_wifi_disconnect(")
        assert "dhcp_watchdog_disarm()" in _function_body(source, "on_got_ip")


class TestTheConsoleContract:
    """frontend/src/boardConsole.ts classifies these exact strings."""

    def test_the_phrases_the_classifier_keys_on(self) -> None:
        source = _code(FF_NET_WIFI_C)
        assert '"no known network in range (' in source
        assert "carries no ssid" in source
        assert '"disconnected (reason %d)' in source
        assert '"associated; waiting for DHCP"' in source
        assert '"wifi sta starting' in source
        assert 'static const char *TAG = "ff-wifi";' in source


class TestTheAnnounce:
    def test_it_emits_both_fields_through_the_seam(self) -> None:
        source = _code(FF_IDENTITY_C)
        assert '"ssid"' in source
        assert '"known_networks"' in source
        assert "ff_net_ssid(" in source
        assert "ff_net_adapter.h" not in FF_IDENTITY_C.read_text()
        assert "const char *ff_net_ssid(void);" in _code(FF_NET_INTERNAL_H)

    def test_they_sit_right_after_link_type(self) -> None:
        body = _function_body(_code(FF_IDENTITY_C), "announce_object")
        link = body.index('"link_type"')
        ssid = body.index('"ssid"')
        known = body.index('"known_networks"')
        power = body.index('"power_class"')
        assert link < ssid < known < power

    def test_no_passphrase_reaches_the_announce(self) -> None:
        assert "psk" not in _code(FF_IDENTITY_C)


class TestTheAdapterStartsOnce:
    def test_a_retry_cannot_restart_the_adapter(self) -> None:
        source = _code(FF_NET_C)
        body = _function_body(source, "ff_net_bring_up")
        guard = body.index("if (!s_started)")
        assert guard < body.index("ff_net_wifi_start(")
        assert guard < body.index("ff_net_openeth_start(")
        assert "s_started = true;" in body
        # Set only after a successful start: a failed first start is attempted again.
        assert body.index("if (err != ESP_OK)") < body.index("s_started = true;")

    def test_the_address_bit_is_cleared_only_before_the_first_start(self) -> None:
        body = _function_body(_code(FF_NET_C), "ff_net_bring_up")
        clear = body.index("xEventGroupClearBits(")
        assert body.count("xEventGroupClearBits(") == 1
        assert body.index("if (!s_started)") < clear < body.index("s_started = true;")


class TestTheConfirmTimerIsUntouched:
    def test_it_is_still_the_first_statement_of_app_main(self) -> None:
        body = _function_body(_code(AGENT_MAIN_C), "app_main")
        assert body.strip().startswith("ff_mqtt_arm_confirm_timer();")

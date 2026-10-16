"""The `fleetforge` ESP-IDF component (R3-fw-2) — its public surface, pinned as text.

The protocol moved out of `agent/main/` into `agent/components/fleetforge/`, and the agent
became the component's first consumer. What ships in the component's `include/` is
ADDITIVE-ONLY from that commit on, exactly like the wire protocol: a firmware built on it
goes to boards nobody can reach again. So the rule is held here, on every `just test`,
rather than by review (DECISIONS 2026-10-08, R3-fw-2):

* the public headers are exactly the `ff_*.h` headers `agent_main.c` includes, plus the
  maker-API headers (`MAKER_HEADERS`);
* every function the non-maker headers declare is one `agent_main.c` calls, and every
  `ff_*` call in `agent_main.c` is declared in one of them — the agent's surface is what
  the first consumer uses, no more and no less;
* a maker-API header is one a firmware's OWN code calls (R4-fw-2, DECISIONS 2026-10-15):
  the stock agent has no application, so it never includes one, and both worked examples
  (`basic_idf/main/main.c` directly, the Arduino sketch through `Fleetforge.cpp`) call
  every function it declares;
* the calls that would let a firmware lie (publish `confirmed`, claim `rollback_capable`,
  build its own announce, drive the OTA or the transaction record) are component-private;
* `main` reaches the component only through `include/`;
* FF_ROLLBACK_TEST moved with `ff_mqtt.c` (a define left on main would never reach it, and
  a rollback test build would silently confirm);
* `idf_component.yml` carries no `version:` — the single source is `agent/version.txt`.

Same idiom as the other agent tripwires: comments are stripped before any grep, because
the comments quote the spellings the code must not contain.
"""

import re
from pathlib import Path

from tests.agent_src import (
    AGENT_DIR,
    AGENT_MAIN_C,
    AGENT_MAIN_DIR,
    ARDUINO_WRAPPER_CPP,
    COMPONENT_CMAKE,
    COMPONENT_INCLUDE,
    COMPONENT_MANIFEST,
    COMPONENT_SRC,
    IDF_EXAMPLE_MAIN_C,
    MAIN_CMAKE,
)
from tests.test_agent_fault_injection import _cmake_code
from tests.test_ff_cfg import _code

JUSTFILE = AGENT_DIR.parent / "justfile"

PRIVATE_WHOLE_HEADERS = ("ff_ota.h", "ff_txn.h", "ff_net_adapter.h", "ff_marker.h")
INTERNAL_HEADERS = tuple(
    f"ff_{name}_internal.h"
    for name in (
        "identity",
        "mqtt",
        "net",
        "safe_mode",
        "safe_window",
        "selftest",
        "store",
        "telemetry",
        "time",
    )
)
# The maker API (R4-fw-2, DECISIONS 2026-10-15): public headers for a firmware's own code.
# The stock agent has no application and no honest reason to call them, so they are the
# one exception to "include/ is what agent_main.c includes"; the examples prove them instead.
# R5-fw-1 (ff_selftest.h) and R5-fw-5 (ff_safe_window.h) joined it; R4-fw-3 (ff_logf) joins
# this list.
MAKER_HEADERS = ("ff_safe_window.h", "ff_selftest.h", "ff_telemetry.h")

# The spellings that must never become public API. Each one is a way for a firmware built
# on the component to report something that did not happen.
HAZARDS = (
    "ff_mqtt_publish_status",  # publish `confirmed` for an image that never confirmed
    "ff_identity_note_rollback_capable",  # CLAIM rollback_capable (measured, never claimed)
    "ff_identity_note_selftest",  # CLAIM the selftest capability (R5-fw-2)
    "ff_identity_note_safe_window",  # CLAIM the safe_window capability (R5-fw-5)
    "ff_store_save_rollback_capable",
    "ff_identity_announce_json",  # the announce is the component's, not the firmware's
    "ff_ota_start",  # the A/B apply path runs only from the session
    "ff_txn_save",  # the record that crosses the apply reboot
    "FF_STATUS_CONFIRMED",
    "FF_PARTITION_LAYOUT",  # the three-way layout contract
)


def _public_headers() -> list[Path]:
    headers = sorted(COMPONENT_INCLUDE.glob("*.h"))
    assert headers, "the component has no public headers"
    return headers


def _declared_functions(header: Path) -> set[str]:
    """`ff_*` names followed by `(` in a header, with comments and `#` lines removed: a
    header here holds prototypes, types and macros, so what is left is the prototypes.
    Function-pointer typedefs are removed too (R5-fw-1): in
    `typedef ff_selftest_result_t (*ff_selftest_fn)(...);` the return type is followed by
    `(`, and it is a type, not a function anyone could call."""
    code = "\n".join(
        line for line in _code(header).splitlines() if not line.lstrip().startswith("#")
    )
    code = re.sub(r"\btypedef\s+\w+\s*\(\s*\*\s*\w+\s*\)\s*\([^;]*\)\s*;", "", code)
    return set(re.findall(r"\b(ff_[a-z0-9_]+)\s*\(", code))


def _main_included_ff_headers() -> set[str]:
    return set(re.findall(r'^#include "(ff_[a-z0-9_]+\.h)"', AGENT_MAIN_C.read_text(), re.M))


def _main_calls() -> set[str]:
    return set(re.findall(r"\b(ff_[a-z0-9_]+)\s*\(", _code(AGENT_MAIN_C)))


def _agent_headers() -> list[Path]:
    return [path for path in _public_headers() if path.name not in MAKER_HEADERS]


class TestThePublicSurface:
    def test_public_headers_are_exactly_what_agent_main_includes(self) -> None:
        public = {path.name for path in _public_headers()}
        included = _main_included_ff_headers()
        assert included, "agent_main.c includes no ff_*.h header"
        assert set(MAKER_HEADERS) <= public, "a maker header is listed but not public"
        assert public - set(MAKER_HEADERS) == included

    def test_agent_main_includes_no_maker_header(self) -> None:
        """The stock agent has no application: a maker call there would be a fake one."""
        included = _main_included_ff_headers()
        assert included.isdisjoint(MAKER_HEADERS), sorted(included & set(MAKER_HEADERS))
        main = _code(AGENT_MAIN_C)
        for header in MAKER_HEADERS:
            for name in _declared_functions(COMPONENT_INCLUDE / header):
                assert not re.search(rf"\b{name}\s*\(", main), name

    def test_every_maker_function_is_called_by_both_examples(self) -> None:
        idf_example = _code(IDF_EXAMPLE_MAIN_C)
        arduino_wrapper = _code(ARDUINO_WRAPPER_CPP)
        for header in MAKER_HEADERS:
            functions = _declared_functions(COMPONENT_INCLUDE / header)
            assert functions, f"{header} declares no function"
            for name in functions:
                assert re.search(rf"\b{name}\s*\(", idf_example), f"basic_idf: {name}"
                assert re.search(rf"\b{name}\s*\(", arduino_wrapper), f"Fleetforge.cpp: {name}"

    def test_every_public_function_is_called_by_agent_main(self) -> None:
        main = _code(AGENT_MAIN_C)
        declared: set[str] = set()
        for header in _agent_headers():
            functions = _declared_functions(header)
            assert functions, f"{header.name} declares no function"
            declared |= functions
        assert len(declared) >= 16, sorted(declared)
        unused = sorted(name for name in declared if not re.search(rf"\b{name}\s*\(", main))
        assert unused == [], f"public but not used by agent_main.c: {unused}"

    def test_every_call_in_agent_main_is_declared_publicly(self) -> None:
        calls = _main_calls()
        assert calls, "agent_main.c calls no ff_* function"
        declared = set().union(*(_declared_functions(header) for header in _public_headers()))
        assert calls <= declared, f"called but not public: {sorted(calls - declared)}"

    def test_the_hazards_are_private(self) -> None:
        public_code = "\n".join(_code(header) for header in _public_headers())
        private_code = "\n".join(_code(header) for header in sorted(COMPONENT_SRC.glob("*.h")))
        assert private_code, "the component has no private headers"
        for name in HAZARDS:
            assert not re.search(rf"\b{name}\b", public_code), f"{name} is public"
            assert re.search(rf"\b{name}\b", private_code), f"{name} is declared nowhere"

    def test_private_headers_live_in_src_only(self) -> None:
        for name in (*PRIVATE_WHOLE_HEADERS, *INTERNAL_HEADERS):
            assert (COMPONENT_SRC / name).is_file(), name
            assert not (COMPONENT_INCLUDE / name).exists(), name
            assert not (AGENT_MAIN_DIR / name).exists(), name

    def test_no_umbrella_header(self) -> None:
        """agent_main.c would not include it, so it would break the subset rule."""
        assert not (COMPONENT_INCLUDE / "fleetforge.h").exists()


class TestMainIsAConsumer:
    def test_main_holds_only_the_app(self) -> None:
        assert AGENT_MAIN_C.is_file()
        assert sorted(AGENT_MAIN_DIR.glob("ff_*.[ch]")) == []

    def test_main_requires_the_component_and_sees_only_its_public_dir(self) -> None:
        cmake = _cmake_code(MAIN_CMAKE)
        requires = re.search(r"\bREQUIRES\b([^)]*)", cmake)
        assert requires is not None
        assert "fleetforge" in requires.group(1).split()
        srcs = re.search(r"\bSRCS\b(.*?)(?=\b[A-Z_]+_DIRS\b|\bREQUIRES\b)", cmake, re.S)
        assert srcs is not None
        assert re.findall(r'"([^"]+)"', srcs.group(1)) == ["agent_main.c"]
        assert "components/" not in cmake
        assert "src" not in re.findall(r'"([^"]+)"', cmake)
        assert "FF_ROLLBACK_TEST" not in cmake, "it moved with ff_mqtt.c"


class TestTheComponentBuild:
    def test_include_is_public_and_src_is_private(self) -> None:
        cmake = _cmake_code(COMPONENT_CMAKE)
        assert re.search(r'\bINCLUDE_DIRS\s+"include"', cmake)
        assert re.search(r'\bPRIV_INCLUDE_DIRS\s+"src"', cmake)
        assert "-Wall -Wextra -Werror" in cmake

    def test_srcs_are_every_c_file_in_src(self) -> None:
        cmake = _cmake_code(COMPONENT_CMAKE)
        listed = set(re.findall(r'"src/([a-z0-9_]+\.c)"', cmake))
        on_disk = {path.name for path in COMPONENT_SRC.glob("*.c")}
        assert {"ff_mqtt.c", "ff_ota.c", "ff_txn.c"} <= on_disk
        assert listed == on_disk

    def test_the_rollback_test_define_reaches_ff_mqtt(self) -> None:
        """Only ff_mqtt.c reads FF_ROLLBACK_TEST, and a define is per target: on main's
        target it would never reach this component, and an FF_ROLLBACK_TEST=1 build would
        confirm — the live rollback test would then prove nothing."""
        cmake = _cmake_code(COMPONENT_CMAKE)
        assert cmake.count("FF_ROLLBACK_TEST=1") == 1
        assert re.search(
            r"if\(FF_ROLLBACK_TEST\)\s*"
            r"target_compile_definitions\(\$\{COMPONENT_LIB\} PRIVATE FF_ROLLBACK_TEST=1\)",
            cmake,
        )
        assert "FF_ROLLBACK_TEST" in _code(COMPONENT_SRC / "ff_mqtt.c")


class TestTheManifest:
    def _lines(self) -> list[str]:
        assert COMPONENT_MANIFEST.is_file()
        return COMPONENT_MANIFEST.read_text().splitlines()

    def _block(self, key: str) -> list[str]:
        lines = self._lines()
        start = lines.index(f"{key}:")
        block = []
        for line in lines[start + 1 :]:
            if line and not line.startswith((" ", "#")):
                break
            if line.strip() and not line.lstrip().startswith("#"):
                block.append(line.strip())
        assert block, f"empty {key} block"
        return block

    def test_targets_are_the_justfile_targets(self) -> None:
        targets = [line.removeprefix("- ").strip() for line in self._block("targets")]
        match = re.search(r'^agent_targets := "([^"]+)"', JUSTFILE.read_text(), re.M)
        assert match is not None
        assert targets == match.group(1).split()

    def test_it_depends_on_idf_only(self) -> None:
        dependencies = self._block("dependencies")
        assert [line.split(":", 1)[0] for line in dependencies] == ["idf"]

    def test_it_has_no_version_of_its_own(self) -> None:
        """The single source is agent/version.txt (DECISIONS 2026-10-08, R3-fw-2)."""
        lines = self._lines()
        assert not any(re.match(r"\s*version\s*:", line) for line in lines)
        comments = "\n".join(line for line in lines if line.lstrip().startswith("#"))
        assert "agent/version.txt" in comments
        assert "DECISIONS" in comments

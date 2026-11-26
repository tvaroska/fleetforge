"""Where the agent's C sources live — one definition for every test that reads them as text.

Not collected (no `test_` prefix). Since R3-fw-2 the protocol is the `fleetforge` ESP-IDF
component under `agent/components/fleetforge/` (public headers in `include/`, every `.c`
and every private header in `src/`), and `agent/main/` holds only the agent's own app,
`agent_main.c`. A test that globbed `agent/main/*.c` before the move would now silently
see one file and pass on nothing, so every glob over the sources goes through
`agent_sources()`, which refuses to come back empty.
"""

import re
from pathlib import Path

AGENT_DIR = Path(__file__).resolve().parent.parent / "agent"
AGENT_MAIN_DIR = AGENT_DIR / "main"
COMPONENT_DIR = AGENT_DIR / "components" / "fleetforge"
COMPONENT_INCLUDE = COMPONENT_DIR / "include"
COMPONENT_SRC = COMPONENT_DIR / "src"
COMPONENT_CMAKE = COMPONENT_DIR / "CMakeLists.txt"
COMPONENT_MANIFEST = COMPONENT_DIR / "idf_component.yml"
MAIN_CMAKE = AGENT_MAIN_DIR / "CMakeLists.txt"
AGENT_MAIN_C = AGENT_MAIN_DIR / "agent_main.c"
# R4-fw-2: the first maker-API header (a firmware's own code calls it; agent_main.c never does).
FF_TELEMETRY_H = COMPONENT_INCLUDE / "ff_telemetry.h"
FF_TELEMETRY_C = COMPONENT_SRC / "ff_telemetry.c"
FF_TELEMETRY_INTERNAL_H = COMPONENT_SRC / "ff_telemetry_internal.h"
# R5-fw-1: the second maker-API header (the self-test entrypoint) and its runner.
FF_SELFTEST_H = COMPONENT_INCLUDE / "ff_selftest.h"
FF_SELFTEST_C = COMPONENT_SRC / "ff_selftest.c"
FF_SELFTEST_INTERNAL_H = COMPONENT_SRC / "ff_selftest_internal.h"
# R5-fw-5: the third maker-API header (the safe-window rule) and its registration.
FF_SAFE_WINDOW_H = COMPONENT_INCLUDE / "ff_safe_window.h"
FF_SAFE_WINDOW_C = COMPONENT_SRC / "ff_safe_window.c"
FF_SAFE_WINDOW_INTERNAL_H = COMPONENT_SRC / "ff_safe_window_internal.h"
# R5-fw-6: rapid-crash safe mode. A PUBLIC, non-maker header: all three boot paths call it.
FF_SAFE_MODE_H = COMPONENT_INCLUDE / "ff_safe_mode.h"
FF_SAFE_MODE_C = COMPONENT_SRC / "ff_safe_mode.c"
FF_SAFE_MODE_INTERNAL_H = COMPONENT_SRC / "ff_safe_mode_internal.h"
# R5-fw-7: the fourth maker-API header (power class and the sleep helpers).
FF_SLEEP_H = COMPONENT_INCLUDE / "ff_sleep.h"
FF_SLEEP_C = COMPONENT_SRC / "ff_sleep.c"
FF_SLEEP_INTERNAL_H = COMPONENT_SRC / "ff_sleep_internal.h"

# R3-fw-3: the same component directory is the Arduino library (library.json), and the
# wrapper is its second consumer. The example and its partitions.csv travel together.
LIBRARY_JSON = COMPONENT_DIR / "library.json"
COMPONENT_README = COMPONENT_DIR / "README.md"
ARDUINO_WRAPPER_CPP = COMPONENT_SRC / "Fleetforge.cpp"
ARDUINO_WRAPPER_H = COMPONENT_SRC / "Fleetforge.h"
LIB_VERSION_H = COMPONENT_SRC / "ff_lib_version.h"
EXAMPLE_DIR = COMPONENT_DIR / "examples" / "Basic"
EXAMPLE_PARTITIONS = EXAMPLE_DIR / "partitions.csv"
EXAMPLE_INO = EXAMPLE_DIR / "Basic.ino"
EXAMPLE_PLATFORMIO_INI = EXAMPLE_DIR / "platformio.ini"
EXAMPLE_README = EXAMPLE_DIR / "README.md"
# R3-fw-4: the ESP-IDF flavour of the worked example. Its partitions.csv is ab-4m-v1 (an IDF
# build of the component announces that id) and, like the sketch's, a flash-time immutable.
IDF_EXAMPLE_DIR = COMPONENT_DIR / "examples" / "basic_idf"
IDF_EXAMPLE_MAIN_C = IDF_EXAMPLE_DIR / "main" / "main.c"
IDF_EXAMPLE_START_C = IDF_EXAMPLE_DIR / "main" / "fleetforge_start.c"
IDF_EXAMPLE_START_H = IDF_EXAMPLE_DIR / "main" / "fleetforge_start.h"
IDF_EXAMPLE_MAIN_CMAKE = IDF_EXAMPLE_DIR / "main" / "CMakeLists.txt"
IDF_EXAMPLE_PARTITIONS = IDF_EXAMPLE_DIR / "partitions.csv"
IDF_EXAMPLE_SDKCONFIG = IDF_EXAMPLE_DIR / "sdkconfig.defaults"
IDF_EXAMPLE_CMAKE = IDF_EXAMPLE_DIR / "CMakeLists.txt"
IDF_EXAMPLE_README = IDF_EXAMPLE_DIR / "README.md"
# The QEMU harness lives OUTSIDE agent/: its hybrid compile writes managed_components/
# next to it, and agent/ is the IDF build context and a tree tests scan file by file.
LIB_QEMU_PLATFORMIO_INI = AGENT_DIR.parent / "lib-qemu" / "platformio.ini"


def agent_sources() -> list[Path]:
    """Every `.c` / `.h` the agent firmware is built from: main plus the component."""
    sources = sorted(
        path
        for directory in (AGENT_MAIN_DIR, COMPONENT_INCLUDE, COMPONENT_SRC)
        for path in directory.glob("*.[ch]")
    )
    names = {path.name for path in sources}
    assert sources, "no agent sources found"
    assert {"agent_main.c", "ff_mqtt.c", "ff_ota.c"} <= names, sorted(names)
    return sources


def strip_c_comments_and_literals(source: str) -> str:
    """Strip C comments (`/*...*/`, `//...`) and replace string/char contents with spaces."""
    out: list[str] = []
    i = 0
    n = len(source)
    while i < n:
        if source.startswith("/*", i):
            end = source.find("*/", i + 2)
            if end == -1:
                break
            chunk = source[i : end + 2]
            out.append("\n" * chunk.count("\n"))
            i = end + 2
        elif source.startswith("//", i):
            end = source.find("\n", i + 2)
            if end == -1:
                break
            out.append("\n")
            i = end + 1
        elif source[i] in ('"', "'"):
            quote = source[i]
            out.append(quote)
            i += 1
            while i < n and source[i] != quote:
                if source[i] == "\\" and i + 1 < n:
                    i += 2
                else:
                    if source[i] == "\n":
                        out.append("\n")
                    i += 1
            if i < n:
                out.append(quote)
                i += 1
        else:
            out.append(source[i])
            i += 1
    return "".join(out)


_C_FUNC_HEADER_RE = re.compile(
    r"(?:^|\n)[ \t]*(?:[A-Za-z_][A-Za-z0-9_\s\*]*?\s+\*?)([A-Za-z_][A-Za-z0-9_]*)\s*\([^;{}]*\)\s*\{",
)
_C_CONTROL_KEYWORDS = frozenset({"if", "for", "while", "switch", "sizeof"})


def c_functions(source: str) -> dict[str, str]:
    """Return `{function_name: body_without_outer_braces}` over comment/literal-stripped C."""
    cleaned = strip_c_comments_and_literals(source)
    funcs: dict[str, str] = {}
    for match in _C_FUNC_HEADER_RE.finditer(cleaned):
        name = match.group(1)
        if name in _C_CONTROL_KEYWORDS:
            continue
        pos = match.end()
        depth = 1
        while pos < len(cleaned) and depth > 0:
            if cleaned[pos] == "{":
                depth += 1
            elif cleaned[pos] == "}":
                depth -= 1
            pos += 1
        funcs[name] = cleaned[match.end() : pos - 1]
    return funcs


def c_call_sites(source: str, fn_name: str) -> list[str]:
    """Return sorted enclosing C function names for every call (plain or parenthesised) to `fn_name`."""
    call_re = re.compile(
        rf"(?<![A-Za-z0-9_])(?:{re.escape(fn_name)}|\(+\s*{re.escape(fn_name)}\s*\)+)\s*\("
    )
    ident_re = re.compile(rf"\b{re.escape(fn_name)}\b")
    funcs = c_functions(source)
    sites: list[str] = []
    for enclosing, body in funcs.items():
        calls = list(call_re.finditer(body))
        all_refs = list(ident_re.finditer(body))
        if len(all_refs) != len(calls):
            raise AssertionError(
                f"{fn_name} referenced without a direct call inside {enclosing}() "
                f"(possible function-pointer alias)"
            )
        sites.extend([enclosing] * len(calls))
    return sorted(sites)


def _blank_c_comments_and_literals_preserving_offsets(source: str) -> str:
    """Replace C comments and string/char contents with spaces of identical length."""
    chars = list(source)
    i = 0
    n = len(chars)
    while i < n:
        if source.startswith("/*", i):
            end = source.find("*/", i + 2)
            stop = n if end == -1 else end + 2
            for j in range(i, stop):
                if chars[j] != "\n":
                    chars[j] = " "
            i = stop
        elif source.startswith("//", i):
            end = source.find("\n", i + 2)
            stop = n if end == -1 else end
            for j in range(i, stop):
                chars[j] = " "
            i = stop
        elif chars[i] in ('"', "'"):
            quote = chars[i]
            i += 1
            while i < n and chars[i] != quote:
                if chars[i] == "\\" and i + 1 < n:
                    chars[i] = " "
                    if chars[i + 1] != "\n":
                        chars[i + 1] = " "
                    i += 2
                else:
                    if chars[i] != "\n":
                        chars[i] = " "
                    i += 1
            if i < n:
                i += 1
        else:
            i += 1
    return "".join(chars)


def extract_c_function(source: str, fn_name: str) -> str:
    """Extract the full C definition (signature + `{ ... }`) of `fn_name` from `source`."""
    blanked = _blank_c_comments_and_literals_preserving_offsets(source)
    sig_re = re.compile(
        rf"(?:^|\n)([^\n;{{}}]*?\b{re.escape(fn_name)}\s*\([^;{{}}]*\)\s*\{{)",
    )
    match = sig_re.search(blanked)
    if match is None:
        raise ValueError(f"C function {fn_name!r} not found")
    start = match.start(1)
    pos = match.end(1)
    depth = 1
    while pos < len(blanked) and depth > 0:
        if blanked[pos] == "{":
            depth += 1
        elif blanked[pos] == "}":
            depth -= 1
        pos += 1
    return source[start:pos]


def find_c_function(fn_name: str) -> tuple[Path, str]:
    """T-9: Locate `fn_name` across all agent C sources without hardcoding one `.c` filename."""
    matches: list[tuple[Path, str]] = []
    for path in agent_sources():
        if path.suffix != ".c":
            continue
        source = path.read_text(encoding="utf-8")
        try:
            body = extract_c_function(source, fn_name)
        except ValueError:
            continue
        matches.append((path, body))
    if not matches:
        raise ValueError(f"C function {fn_name!r} not found in agent_sources()")
    if len(matches) > 1:
        names = ", ".join(p.name for p, _ in matches)
        raise ValueError(f"C function {fn_name!r} defined in multiple files: {names}")
    return matches[0]

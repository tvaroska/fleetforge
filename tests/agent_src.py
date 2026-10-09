"""Where the agent's C sources live — one definition for every test that reads them as text.

Not collected (no `test_` prefix). Since R3-fw-2 the protocol is the `fleetforge` ESP-IDF
component under `agent/components/fleetforge/` (public headers in `include/`, every `.c`
and every private header in `src/`), and `agent/main/` holds only the agent's own app,
`agent_main.c`. A test that globbed `agent/main/*.c` before the move would now silently
see one file and pass on nothing, so every glob over the sources goes through
`agent_sources()`, which refuses to come back empty.
"""

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

# R3-fw-3: the same component directory is the Arduino library (library.json), and the
# wrapper is its second consumer. The example and its partitions.csv travel together.
LIBRARY_JSON = COMPONENT_DIR / "library.json"
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

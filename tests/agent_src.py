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

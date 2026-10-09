"""The fault-injection builds (R2-test-1) — firmware tripwires, as text.

`FF_FAULT_TEST=bootloop|hang` builds a DELIBERATELY BROKEN agent: one that aborts on every
boot, or one that never reaches its broker session. QEMU is what proves the board recovers.
The hang must now roll back too, because the confirm timer is armed before the hook
(R2-fw-4). What this file holds, on every `just test`, is the
property whose violation would be a fleet incident rather than a failed test:

* **the hook is absent from every normal build.** It is compiled in only through a
  preprocessor guard that only the CMake switch defines. A mis-guarded `abort()` would ship
  a boot-looping agent, and serially flashed that is a board with no rollback at all;
* the switch takes exactly two values and refuses anything else, and refuses to combine
  with FF_ROLLBACK_TEST (one fault per image, or a result cannot be attributed);
* the hook lives in `agent_main.c` alone, between `log_boot_facts()` and `nvs_ready()`.
  `ff_mqtt.c` and `ff_ota.c`, the CRITICAL confirm/rollback and A/B files, never see it;
* no `just` recipe builds with a fault switch, so `agent/dist/` (what gets published) is
  always a clean build.

Same idiom as `test_agent_txn.py`: comments quote the very spellings they guard, so the C
greps look at code only (`_code`), and CMake comments are stripped here.
"""

import re
from pathlib import Path

from tests.agent_src import AGENT_DIR, AGENT_MAIN_C, COMPONENT_CMAKE, MAIN_CMAKE, agent_sources
from tests.test_agent_txn import _function_body
from tests.test_ff_cfg import _code

ROOT = Path(__file__).resolve().parent.parent
AGENT = AGENT_DIR
TOP_CMAKE = AGENT / "CMakeLists.txt"
DOCKERFILE = AGENT / "Dockerfile"
JUSTFILE = ROOT / "justfile"

FAULT_DEFINES = ("FF_FAULT_TEST_BOOTLOOP", "FF_FAULT_TEST_HANG")


def _cmake_code(path: Path) -> str:
    """CMake text with `#` comments removed (none of these files puts `#` in a string)."""
    return "\n".join(line.split("#", 1)[0].rstrip() for line in path.read_text().splitlines())


def _fault_block(cmake: str) -> str:
    """The top-level `if(DEFINED FF_FAULT_TEST ...)` ... matching `endif()`."""
    start = cmake.index('if(DEFINED FF_FAULT_TEST AND NOT FF_FAULT_TEST STREQUAL "")')
    depth = 0
    for match in re.finditer(r"\b(if|endif)\s*\(", cmake[start:]):
        depth += 1 if match.group(1) == "if" else -1
        if depth == 0:
            return cmake[start : start + match.end()]
    raise AssertionError("unbalanced if()/endif() around FF_FAULT_TEST")


class TestTheSwitch:
    def test_two_values_two_suffixes_and_a_fatal_error_for_anything_else(self) -> None:
        block = _fault_block(_cmake_code(TOP_CMAKE))
        accepted = re.findall(r'FF_FAULT_TEST STREQUAL "([a-z]+)"', block)
        assert accepted == ["bootloop", "hang"]
        assert 'string(APPEND PROJECT_VER "-bltest")' in block
        assert 'string(APPEND PROJECT_VER "-hangtest")' in block
        assert "else()" in block
        assert re.search(r"else\(\)\s*message\(FATAL_ERROR", block), (
            "an unknown value must fail the build, not build a normal agent"
        )

    def test_exclusive_with_the_rollback_test(self) -> None:
        block = _fault_block(_cmake_code(TOP_CMAKE))
        assert re.search(
            r"if\(FF_ROLLBACK_TEST\)\s*message\(FATAL_ERROR\s*\"FF_FAULT_TEST and "
            r"FF_ROLLBACK_TEST are exclusive",
            block,
        )

    def test_the_switch_is_read_before_the_project_is(self) -> None:
        """PROJECT_VER must be final before project() bakes it into the app descriptor."""
        cmake = _cmake_code(TOP_CMAKE)
        assert cmake.index("FF_FAULT_TEST") < cmake.index("include($ENV{IDF_PATH}")

    def test_the_defines_exist_only_inside_their_strequal_blocks(self) -> None:
        cmake = _cmake_code(MAIN_CMAKE)
        for define, value in zip(FAULT_DEFINES, ("bootloop", "hang"), strict=True):
            assert cmake.count(f"{define}=1") == 1, define
            guarded = re.search(
                rf'if\(FF_FAULT_TEST STREQUAL "{value}"\)\s*'
                rf"target_compile_definitions\(\$\{{COMPONENT_LIB\}} PRIVATE {define}=1\)",
                cmake,
            )
            assert guarded, f"{define} must be defined only when FF_FAULT_TEST is {value}"

    def test_the_dockerfile_defaults_to_empty_and_passes_it_through(self) -> None:
        dockerfile = DOCKERFILE.read_text()
        assert re.search(r"^ARG FF_FAULT_TEST=$", dockerfile, flags=re.MULTILINE), (
            "the default must be EMPTY: a normal `docker build` is a normal agent"
        )
        assert "-DFF_FAULT_TEST=${FF_FAULT_TEST}" in dockerfile


def _guarded_regions(source: str) -> list[tuple[int, int]]:
    """`(start, end)` offsets of every `#if FF_FAULT_TEST_BOOTLOOP` … `#endif` region.

    A small preprocessor scanner: tracks nesting so an inner `#if`/`#endif` cannot close
    the region early. The region includes its `#elif FF_FAULT_TEST_HANG` branch.
    """
    regions: list[tuple[int, int]] = []
    stack: list[tuple[int, bool]] = []
    for match in re.finditer(
        r"^[ \t]*#[ \t]*(if|ifdef|ifndef|elif|else|endif)\b(.*)$", source, flags=re.MULTILINE
    ):
        directive, rest = match.group(1), match.group(2).strip()
        if directive in ("if", "ifdef", "ifndef"):
            stack.append((match.start(), directive == "if" and rest == FAULT_DEFINES[0]))
        elif directive == "elif":
            assert stack, "#elif without #if"
            if stack[-1][1]:
                assert rest == FAULT_DEFINES[1], f"unexpected branch #elif {rest}"
        elif directive == "endif":
            assert stack, "#endif without #if"
            start, is_fault = stack.pop()
            if is_fault:
                regions.append((start, match.end()))
    assert not stack, "unterminated #if"
    return regions


class TestTheHook:
    def test_every_abort_and_the_hang_sit_inside_the_guard(self) -> None:
        source = _code(AGENT_MAIN_C)
        regions = _guarded_regions(source)
        assert len(regions) == 1, "exactly one fault hook"

        def inside(offset: int) -> bool:
            return any(start < offset < end for start, end in regions)

        sensitive = [m.start() for m in re.finditer(r"\babort\s*\(", source)]
        sensitive += [m.start() for m in re.finditer(r"FF_FAULT_TEST=", source)]
        assert sensitive, "the hook is missing"
        assert all(inside(offset) for offset in sensitive), (
            "an abort() or a fault loop outside #if FF_FAULT_TEST_BOOTLOOP/#elif "
            "FF_FAULT_TEST_HANG would ship in the normal build"
        )
        region = source[regions[0][0] : regions[0][1]]
        assert '"FF_FAULT_TEST=bootloop' in region
        assert '"FF_FAULT_TEST=hang' in region
        assert "park(" not in region, "the hang must not depend on ff_progress"

    def test_the_hook_runs_after_the_boot_facts_and_before_any_of_our_code(self) -> None:
        body = _function_body(_code(AGENT_MAIN_C), "app_main")
        facts = body.index("log_boot_facts();")
        hook = body.index("#if FF_FAULT_TEST_BOOTLOOP")
        nvs = body.index("nvs_ready()")
        assert facts < hook < nvs
        # Nothing but whitespace between the boot facts and the hook.
        assert body[facts + len("log_boot_facts();") : hook].strip() == ""

    def test_the_confirm_timer_is_armed_before_the_hook(self) -> None:
        """What makes `-hangtest` a regression image for R2-fw-4: the hang sits after the
        arm, so an OTA'd hang image must roll itself back."""
        body = _function_body(_code(AGENT_MAIN_C), "app_main")
        assert body.index("ff_mqtt_arm_confirm_timer();") < body.index("#if FF_FAULT_TEST_BOOTLOOP")

    def test_no_other_agent_source_knows_the_switch(self) -> None:
        """Main and the `fleetforge` component (R3-fw-2): the hooks are agent_main.c's
        alone, so the component neither reads the switch nor defines it."""
        sources = agent_sources()
        assert any(path.name == "ff_mqtt.c" for path in sources)
        assert any(path.name == "ff_ota.c" for path in sources)
        for path in sources:
            if path == AGENT_MAIN_C:
                continue
            assert "FF_FAULT_TEST" not in path.read_text(), path.name
        assert "FF_FAULT_TEST" not in COMPONENT_CMAKE.read_text()


def test_no_recipe_builds_a_fault_image() -> None:
    """`agent-build` writes agent/dist/, which `agent-publish` uploads: always clean."""
    for line in JUSTFILE.read_text().splitlines():
        code = "" if line.lstrip().startswith("#") else line
        assert "FF_FAULT_TEST" not in code, line
        assert "FF_ROLLBACK_TEST" not in code, line

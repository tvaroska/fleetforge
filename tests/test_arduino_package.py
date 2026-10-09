"""The Arduino IDE package of the library (R3-fw-8) — `scripts/arduino_package.py`.

The package is a build artifact (`dist/arduino/`, gitignored), generated from the one
source, the component directory. It is a new distribution channel for two CRITICAL
artifacts, so what it ships is pinned here on every `just test` (CRITICAL.md):

* the example's `partitions.csv` (ab-4m-arduino-v1, flash-time immutable) byte-identical;
* `Fleetforge.cpp`'s rollback posture: every packaged file is its source's bytes, a `.c`/
  `.cpp` only gains the LOG_LOCAL_LEVEL prelude in front, and `library.properties` never
  asks for `dot_a_linkage` or `precompiled` (archive linking would leave the strong
  `verifyRollbackLater()` to symbol resolution order);
* `library.properties` comes from library.json, so the version stays one value in three
  files (agent/version.txt, library.json, ff_lib_version.h), never four.

The compile proof (arduino-cli + core 3.3.12) is `just lib-arduino-check`, not a test.
"""

from __future__ import annotations

import importlib.util
import json
import re
import subprocess
import sys
import zipfile
from pathlib import Path
from types import ModuleType
from typing import Any

import pytest

from tests.agent_src import (
    COMPONENT_DIR,
    COMPONENT_INCLUDE,
    COMPONENT_SRC,
    EXAMPLE_INO,
    EXAMPLE_PARTITIONS,
    LIB_VERSION_H,
    LIBRARY_JSON,
)
from tests.test_agent_partitions import _rows
from tests.test_arduino_library import ARDUINO_LAYOUT_SHA256, VERSION_TXT, _fingerprint

REPO_ROOT = COMPONENT_DIR.parents[2]
PACKAGE_PY = REPO_ROOT / "scripts" / "arduino_package.py"

# Retyped, not imported: a tripwire must not read the constants it guards.
PROPERTY_KEYS = [
    "name",
    "version",
    "author",
    "maintainer",
    "sentence",
    "paragraph",
    "category",
    "url",
    "architectures",
    "includes",
]
FORBIDDEN_KEYS = ("dot_a_linkage", "precompiled", "ldflags")


def _load_package() -> ModuleType:
    """By path, like the other script tests."""
    spec = importlib.util.spec_from_file_location("arduino_package", PACKAGE_PY)
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    sys.modules["arduino_package"] = module
    spec.loader.exec_module(module)
    return module


pkg = _load_package()


def _manifest() -> dict[str, Any]:
    loaded: Any = json.loads(LIBRARY_JSON.read_text())
    assert isinstance(loaded, dict)
    return loaded


def _properties(package: Path) -> dict[str, str]:
    pairs: dict[str, str] = {}
    for line in (package / "library.properties").read_text().splitlines():
        key, sep, value = line.partition("=")
        assert sep, line
        assert key not in pairs, key
        pairs[key] = value
    return pairs


def _git_basenames(directory: Path) -> list[str]:
    cmd = ["git", "ls-files", "--", str(directory.relative_to(REPO_ROOT))]
    proc = subprocess.run(cmd, cwd=REPO_ROOT, capture_output=True, text=True, check=True)  # noqa: S603,S607
    return [Path(line).name for line in proc.stdout.splitlines() if line]


def _files(root: Path) -> set[str]:
    return {path.relative_to(root).as_posix() for path in root.rglob("*") if path.is_file()}


@pytest.fixture(scope="module")
def package(tmp_path_factory: pytest.TempPathFactory) -> Path:
    built: Path = pkg.build_package(COMPONENT_DIR, tmp_path_factory.mktemp("arduino"))
    return built


class TestLibraryProperties:
    def test_exactly_the_keys(self, package: Path) -> None:
        assert list(_properties(package)) == PROPERTY_KEYS

    def test_generated_from_library_json(self, package: Path) -> None:
        props = _properties(package)
        manifest = _manifest()
        assert props["name"] == manifest["name"] == "Fleetforge"
        assert props["version"] == manifest["version"]
        assert props["includes"] == manifest["headers"] == "Fleetforge.h"
        assert props["architectures"] == "esp32"
        assert manifest["description"].startswith(props["sentence"])
        assert "3.3.12" in props["paragraph"] and "ab-4m-arduino-v1" in props["paragraph"]

    def test_one_version_in_three_files_and_the_package_reads_it(self, package: Path) -> None:
        """library.properties is generated from library.json: never a fourth version copy."""
        header = re.search(r'#define FF_LIB_VERSION "([^"]+)"', LIB_VERSION_H.read_text())
        assert header is not None
        assert (
            _properties(package)["version"]
            == VERSION_TXT.read_text().strip()
            == _manifest()["version"]
            == header.group(1)
        )
        assert not (COMPONENT_DIR / "library.properties").exists()

    @pytest.mark.parametrize("key", FORBIDDEN_KEYS)
    def test_never_archive_linking(self, package: Path, key: str) -> None:
        """dot_a_linkage/precompiled archive the library; the strong verifyRollbackLater()
        would then be linked only if symbol resolution order happens to pull it."""
        assert key not in _properties(package)
        assert key not in (package / "library.properties").read_text()


class TestTheFlatSrc:
    def test_src_is_include_plus_src_from_git(self, package: Path) -> None:
        tracked = _git_basenames(COMPONENT_INCLUDE) + _git_basenames(COMPONENT_SRC)
        assert len(tracked) == len(set(tracked)), "basename collision in the component"
        src = package / "src"
        assert all(path.is_file() for path in src.iterdir()), "src/ must be flat"
        assert sorted(path.name for path in src.iterdir()) == sorted(tracked)

    def test_headers_are_byte_identical(self, package: Path) -> None:
        for path in [*COMPONENT_INCLUDE.iterdir(), *COMPONENT_SRC.iterdir()]:
            if path.suffix not in (".c", ".cpp"):
                assert (package / "src" / path.name).read_bytes() == path.read_bytes(), path

    def test_sources_are_the_prelude_plus_the_source_bytes(self, package: Path) -> None:
        head = pkg.prelude(_manifest()).encode()
        sources = [p for p in COMPONENT_SRC.iterdir() if p.suffix in (".c", ".cpp")]
        assert sources
        for path in sources:
            assert (package / "src" / path.name).read_bytes() == head + path.read_bytes(), path

    def test_the_prelude_carries_log_local_level_from_library_json(self) -> None:
        flags = _manifest()["build"]["flags"]
        values = [f.split("=", 1)[1] for f in flags if f.startswith("-DLOG_LOCAL_LEVEL=")]
        assert len(values) == 1
        head = pkg.prelude(_manifest())
        assert f"#ifndef LOG_LOCAL_LEVEL\n#define LOG_LOCAL_LEVEL {values[0]}\n#endif\n" in head
        assert head.endswith("#line 1\n"), "diagnostics must keep the source's line numbers"

    def test_the_rollback_posture_ships(self, package: Path) -> None:
        source = (package / "src" / "Fleetforge.cpp").read_text()
        assert re.search(
            r'extern "C" bool verifyRollbackLater\(void\)\s*\{\s*return true;\s*\}', source
        )


class TestTheExample:
    def test_exactly_the_sketch_and_its_table(self, package: Path) -> None:
        assert _files(package / "examples") == {"Basic/Basic.ino", "Basic/partitions.csv"}

    def test_byte_identical(self, package: Path) -> None:
        basic = package / "examples" / "Basic"
        assert (basic / "Basic.ino").read_bytes() == EXAMPLE_INO.read_bytes()
        assert (basic / "partitions.csv").read_bytes() == EXAMPLE_PARTITIONS.read_bytes()

    def test_the_packaged_table_is_ab_4m_arduino_v1(self, package: Path) -> None:
        rows = _rows(package / "examples" / "Basic" / "partitions.csv")
        assert _fingerprint(rows) == ARDUINO_LAYOUT_SHA256

    def test_nothing_else_at_the_root(self, package: Path) -> None:
        assert sorted(path.name for path in package.iterdir()) == [
            "examples",
            "library.properties",
            "src",
        ]


class TestTheFlags:
    @pytest.mark.parametrize("flag", ["-O2", "-include x.h", "-Ifoo", "-UNDEBUG", "-D 1BAD"])
    def test_an_unknown_flag_raises(self, flag: str) -> None:
        manifest = {**_manifest(), "build": {"flags": ["-Wall", flag]}}
        with pytest.raises(pkg.PackageError):
            pkg.prelude(manifest)

    def test_warning_flags_are_accepted_and_carry_nothing(self) -> None:
        manifest = {**_manifest(), "build": {"flags": ["-Wall", "-Wextra", "-Werror"]}}
        assert "#define" not in pkg.prelude(manifest)

    def test_an_unknown_flag_stops_the_build(self, tmp_path: Path) -> None:
        component = _fake_component(tmp_path / "c", flags=["-Wall", "-ffast-math"])
        with pytest.raises(pkg.PackageError, match="-ffast-math"):
            pkg.build_package(component, tmp_path / "out")


def _fake_component(root: Path, flags: list[str], collide: bool = False) -> Path:
    (root / "include").mkdir(parents=True)
    (root / "src").mkdir()
    (root / "examples" / "Basic").mkdir(parents=True)
    manifest = {**_manifest(), "build": {"flags": flags}}
    (root / "library.json").write_text(json.dumps(manifest))
    (root / "include" / "x.h").write_text("/* public */\n")
    (root / "src" / "x.c").write_text("int x;\n")
    if collide:
        (root / "src" / "x.h").write_text("/* private */\n")
    (root / "examples" / "Basic" / "Basic.ino").write_text("void setup(){}\nvoid loop(){}\n")
    (root / "examples" / "Basic" / "partitions.csv").write_bytes(EXAMPLE_PARTITIONS.read_bytes())
    return root


class TestCollisions:
    def test_a_basename_collision_raises(self, tmp_path: Path) -> None:
        component = _fake_component(tmp_path / "c", flags=["-Wall"], collide=True)
        with pytest.raises(pkg.PackageError, match="collision"):
            pkg.build_package(component, tmp_path / "out")

    def test_the_fake_component_builds_without_one(self, tmp_path: Path) -> None:
        component = _fake_component(tmp_path / "c", flags=["-Wall"])
        package = pkg.build_package(component, tmp_path / "out")
        assert _files(package / "src") == {"x.h", "x.c"}


class TestTheZip:
    def test_one_root_and_the_package_file_set(self, package: Path, tmp_path: Path) -> None:
        archive = pkg.make_zip(package, tmp_path / "Fleetforge.zip")
        with zipfile.ZipFile(archive) as opened:
            names = opened.namelist()
            assert {name.split("/", 1)[0] for name in names} == {"Fleetforge"}
            files = {n.removeprefix("Fleetforge/") for n in names if not n.endswith("/")}
            assert files == _files(package)
            for name in files:
                assert opened.read(f"Fleetforge/{name}") == (package / name).read_bytes()

    def test_reproducible_bytes(self, tmp_path: Path) -> None:
        first = pkg.make_zip(
            pkg.build_package(COMPONENT_DIR, tmp_path / "a"), tmp_path / "a" / "F.zip"
        )
        second = pkg.make_zip(
            pkg.build_package(COMPONENT_DIR, tmp_path / "b"), tmp_path / "b" / "F.zip"
        )
        assert first.read_bytes() == second.read_bytes()

    def test_rebuild_wipes_the_old_package(self, tmp_path: Path) -> None:
        stale = tmp_path / "Fleetforge" / "src" / "stale.c"
        stale.parent.mkdir(parents=True)
        stale.write_text("int stale;\n")
        pkg.build_package(COMPONENT_DIR, tmp_path)
        assert not stale.exists()

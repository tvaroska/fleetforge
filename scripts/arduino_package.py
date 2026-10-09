#!/usr/bin/env python3
"""Package the Fleetforge library for the Arduino IDE (R3-fw-8).

The component directory `agent/components/fleetforge/` is the ESP-IDF component AND the
PlatformIO library, and it stays the one source. The Arduino IDE cannot build it as it is:
the 1.5 library format compiles and includes `src/` only, so `include/` is invisible. This
script writes a generated, never checked-in package of it:

    dist/arduino/Fleetforge/
        library.properties        generated from library.json (no fourth version copy)
        src/                      include/ + src/ of the component, flat, same basenames
        examples/Basic/Basic.ino
        examples/Basic/partitions.csv   ab-4m-arduino-v1, byte-identical (CRITICAL.md)
    dist/arduino/Fleetforge-<version>.zip   one root folder Fleetforge/, reproducible bytes

Every packaged file is byte-identical to its source, with one exception: each `.c`/`.cpp`
gets a short prelude prepended, because the IDE has no per-library build flags. The
prelude carries library.json's `-DNAME=VALUE` flags (today only LOG_LOCAL_LEVEL, without
which every ESP_LOGI of the library is compiled out) and ends in `#line 1`, so compiler
diagnostics keep the original line numbers. Any library.json flag this script does not
know how to carry is an error, never silently dropped.

Never `dot_a_linkage` and never `precompiled`: the strong `verifyRollbackLater()` in
Fleetforge.cpp must be linked as an object, not fished out of an archive by symbol
resolution order (DECISIONS 2026-10-08, R3-fw-3; CRITICAL.md).

Stdlib only. `just lib-arduino-package`; proved by `just lib-arduino-check`.
"""

from __future__ import annotations

import argparse
import json
import re
import shutil
import sys
import zipfile
from pathlib import Path
from typing import Any

REPO_ROOT = Path(__file__).resolve().parent.parent
COMPONENT_DIR = REPO_ROOT / "agent" / "components" / "fleetforge"
DEFAULT_OUT = REPO_ROOT / "dist" / "arduino"

PACKAGE_NAME = "Fleetforge"
# The two directories of the component that make up the library's code, flattened.
CODE_DIRS = ("include", "src")
# The example a persona opens (File -> Examples -> Fleetforge -> Basic). Not platformio.ini
# (its `symlink://../..` is wrong inside a package), not the PlatformIO README, not basic_idf.
EXAMPLE_FILES = ("Basic/Basic.ino", "Basic/partitions.csv")
SOURCE_SUFFIXES = (".c", ".cpp")

# library.json build.flags with no package equivalent; `arduino_ide_check.py` compiles with
# `--warnings all` and fails on any warning from the library instead.
WARNING_FLAGS = frozenset({"-Wall", "-Wextra", "-Werror"})
DEFINE_FLAG = re.compile(r"-D([A-Za-z_][A-Za-z0-9_]*)(?:=(.*))?")

AUTHOR = "Boris Tvaroska"
URL = "https://github.com/tvaroska/fleetforge"
CORE_NOTE = (
    "Needs Arduino-ESP32 core 3.3.12 and the example's partitions.csv "
    "(layout ab-4m-arduino-v1) next to your sketch."
)
PRELUDE_HEAD = (
    "/* Prepended by scripts/arduino_package.py: library.json build.flags "
    "(the Arduino IDE has no per-library flags). */\n"
)

# Fixed metadata for every zip entry: same inputs, same bytes.
ZIP_DATE = (1980, 1, 1, 0, 0, 0)
FILE_MODE = 0o100644
DIR_MODE = 0o040755


class PackageError(Exception):
    """The package cannot be built as asked; the message is the one line the CLI prints."""


def read_manifest(component_dir: Path) -> dict[str, Any]:
    path = component_dir / "library.json"
    try:
        loaded: Any = json.loads(path.read_text())
    except (OSError, json.JSONDecodeError) as exc:
        raise PackageError(f"{path}: {exc}") from exc
    if not isinstance(loaded, dict):
        raise PackageError(f"{path} is not a JSON object")
    for key in ("name", "version", "description", "headers"):
        if not isinstance(loaded.get(key), str) or not loaded[key]:
            raise PackageError(f"{path} has no string `{key}`")
    return loaded


def _single_line(value: str, key: str) -> str:
    if "\n" in value or "\r" in value:
        raise PackageError(f"library.properties `{key}` must be one line")
    return value.strip()


def library_properties(manifest: dict[str, Any]) -> str:
    """The 1.5-format `library.properties`, every value taken from library.json or fixed.

    `sentence` is the description up to and including its first ". "; `paragraph` is the
    rest plus the core and partition-table note (a library.properties cannot pin a core)."""
    description = _single_line(str(manifest["description"]), "description")
    head, sep, rest = description.partition(". ")
    sentence = head + "." if sep else description
    paragraph = f"{rest.strip()} {CORE_NOTE}".strip()
    fields = [
        ("name", _single_line(str(manifest["name"]), "name")),
        ("version", _single_line(str(manifest["version"]), "version")),
        ("author", AUTHOR),
        ("maintainer", AUTHOR),
        ("sentence", sentence),
        ("paragraph", paragraph),
        ("category", "Communication"),
        ("url", URL),
        ("architectures", "esp32"),
        # Without it "Include Library" pastes an #include for every header in src/.
        ("includes", _single_line(str(manifest["headers"]), "headers")),
    ]
    return "".join(f"{key}={value}\n" for key, value in fields)


def _flags(manifest: dict[str, Any]) -> list[str]:
    build = manifest.get("build", {})
    if not isinstance(build, dict):
        raise PackageError("library.json `build` is not an object")
    flags = build.get("flags", [])
    if isinstance(flags, str):
        flags = flags.split()
    if not isinstance(flags, list) or not all(isinstance(flag, str) for flag in flags):
        raise PackageError("library.json `build.flags` is not a list of strings")
    return [token for flag in flags for token in flag.split()]


def prelude(manifest: dict[str, Any]) -> str:
    """What every packaged `.c`/`.cpp` starts with: library.json's `-D` flags as guarded
    `#define`s, then `#line 1` so the source's own line numbers survive.

    A flag that is neither a `-D` nor one of the warning flags raises PackageError: a future
    library.json flag must not silently disappear from the IDE build."""
    lines = [PRELUDE_HEAD]
    for flag in _flags(manifest):
        if flag in WARNING_FLAGS:
            continue
        match = DEFINE_FLAG.fullmatch(flag)
        if match is None:
            raise PackageError(
                f"library.json build flag {flag!r} has no Arduino IDE equivalent; "
                "teach scripts/arduino_package.py to carry it or drop it"
            )
        name, value = match.group(1), match.group(2)
        lines.append(f"#ifndef {name}\n#define {name} {'1' if value is None else value}\n#endif\n")
    lines.append("#line 1\n")
    return "".join(lines)


def _regular_files(directory: Path) -> list[Path]:
    if not directory.is_dir():
        raise PackageError(f"{directory} is missing")
    nested = [path for path in directory.iterdir() if path.is_dir()]
    if nested:
        # A flat src/ has no room for a subtree; a new subdirectory needs a decision here.
        raise PackageError(f"{nested[0]}: subdirectories are not packaged; flatten it first")
    return sorted(path for path in directory.iterdir() if path.is_file())


def build_package(component_dir: Path, out_dir: Path) -> Path:
    """Wipe and recreate `out_dir/Fleetforge` from `component_dir`; return that directory."""
    manifest = read_manifest(component_dir)
    head = prelude(manifest).encode("ascii")

    sources: dict[str, Path] = {}
    for sub in CODE_DIRS:
        for path in _regular_files(component_dir / sub):
            if path.name in sources:
                raise PackageError(
                    f"basename collision in the flat src/: {sources[path.name]} and {path}"
                )
            sources[path.name] = path

    examples: list[tuple[str, Path]] = []
    for rel in EXAMPLE_FILES:
        path = component_dir / "examples" / rel
        if not path.is_file():
            raise PackageError(f"{path} is missing")
        examples.append((rel, path))

    package = out_dir / PACKAGE_NAME
    if package.exists():
        shutil.rmtree(package)
    (package / "src").mkdir(parents=True)

    (package / "library.properties").write_text(library_properties(manifest))
    for name, path in sorted(sources.items()):
        body = path.read_bytes()
        if path.suffix in SOURCE_SUFFIXES:
            body = head + body
        (package / "src" / name).write_bytes(body)
    for rel, path in examples:
        target = package / "examples" / rel
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_bytes(path.read_bytes())
    return package


def make_zip(package_dir: Path, zip_path: Path) -> Path:
    """Zip `package_dir` under its own name as the single root folder: sorted entries, fixed
    timestamps and modes, so the same package always gives the same bytes."""
    root = package_dir.name
    entries = sorted(package_dir.rglob("*"))
    zip_path.parent.mkdir(parents=True, exist_ok=True)
    zip_path.unlink(missing_ok=True)
    with zipfile.ZipFile(zip_path, "w") as archive:
        for rel, path in [
            ("", package_dir),
            *((p.relative_to(package_dir).as_posix(), p) for p in entries),
        ]:
            arcname = f"{root}/{rel}" if rel else root
            if path.is_dir():
                info = zipfile.ZipInfo(arcname + "/", date_time=ZIP_DATE)
                info.external_attr = DIR_MODE << 16
                info.create_system = 3
                archive.writestr(info, b"")
            else:
                info = zipfile.ZipInfo(arcname, date_time=ZIP_DATE)
                info.external_attr = FILE_MODE << 16
                info.create_system = 3
                info.compress_type = zipfile.ZIP_DEFLATED
                archive.writestr(info, path.read_bytes(), compresslevel=9)
    return zip_path


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0] if __doc__ else None)
    parser.add_argument(
        "--out", type=Path, default=DEFAULT_OUT, help="output directory (default dist/arduino)"
    )
    args = parser.parse_args(argv)
    out = args.out if args.out.is_absolute() else REPO_ROOT / args.out
    try:
        version = read_manifest(COMPONENT_DIR)["version"]
        package = build_package(COMPONENT_DIR, out)
        archive = make_zip(package, out / f"{PACKAGE_NAME}-{version}.zip")
    except PackageError as exc:
        print(f"arduino_package: {exc}", file=sys.stderr)
        return 1
    files = sum(1 for path in package.rglob("*") if path.is_file())
    print(f"package: {package}")
    print(f"zip:     {archive}")
    print(f"version: {version}, {files} files")
    return 0


if __name__ == "__main__":
    sys.exit(main())

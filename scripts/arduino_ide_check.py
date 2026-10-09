#!/usr/bin/env python3
"""Prove the Arduino IDE package (R3-fw-8): install it like a persona and compile Basic.

What a maker does, in a scratch toolchain that never touches `$HOME`:

* `arduino-cli` 1.5.1, pinned by URL and sha256 (checked before extracting), plus the
  Espressif core `esp32:esp32@3.3.12` from the Boards Manager index (~7.8 GB, pruned to the
  Xtensa targets afterwards, docs/runbooks/arduino-partition-measurement.md);
* the package built fresh by `scripts/arduino_package.py` into a temp dir (never the
  `dist/` a previous run left), zipped, and installed with `lib install --zip-path`, the
  CLI twin of *Sketch -> Include Library -> Add .ZIP Library*;
* the installed `examples/Basic` copied to a fresh sketch folder (the IDE's *Save As*) and
  compiled with each board's DEFAULT menu options: `esp32:esp32:esp32`, `esp32:esp32:esp32s3`.

Per board: compile exit 0; zero `warning:` lines from the library or the sketch under
`--warnings all` (library.json's -Wall -Wextra -Werror have no IDE equivalent); the
"Used library" table names Fleetforge <version> at the installed path; the app fits the
1966080-byte slot; the BUILT partitions.bin fingerprints to ab-4m-arduino-v1 (the
sketch-local table won); flash_args writes the app at 0x10000 and boot_app0 at 0xe000; the
ELF has a strong `T verifyRollbackLater` (CRITICAL.md: without it initArduino() confirms
every OTA'd image before setup()); the app carries the ff-lib INFO format string (the
LOG_LOCAL_LEVEL prelude reached the build). The `Maximum is N bytes` line is recorded only:
it is the board menu's number, not a layout check.

    just lib-arduino-check                    # full run, toolchain deleted at the end
    just lib-arduino-check --keep-toolchain   # reuse /tmp/ff-arduino-ide across reruns
    just lib-arduino-check --negative-control # package WITHOUT the prelude: must FAIL (h)

Stdlib only; exit 0 pass, 1 fail, 2 setup refused (disk, download).
"""

from __future__ import annotations

import argparse
import hashlib
import importlib.util
import json
import re
import shutil
import subprocess
import sys
import tarfile
import tempfile
import time
import urllib.request
from collections.abc import Sequence
from dataclasses import dataclass, field
from pathlib import Path
from types import ModuleType
from typing import Any

REPO_ROOT = Path(__file__).resolve().parent.parent
if str(REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(REPO_ROOT))
from scripts import arduino_package  # noqa: E402

TOOLS_DIR = REPO_ROOT / "agent" / "tools"

CLI_VERSION = "1.5.1"
CLI_URL = f"https://downloads.arduino.cc/arduino-cli/arduino-cli_{CLI_VERSION}_Linux_64bit.tar.gz"
CLI_SHA256 = "28a8e119c498a25607821c36cb2dc49e8463941b261a0d99091baa7bc692dd2b"
ESPRESSIF_INDEX = "https://espressif.github.io/arduino-esp32/package_esp32_index.json"
CORE = "esp32:esp32"
CORE_VERSION = "3.3.12"
BOARDS = (("esp32", "esp32:esp32:esp32"), ("esp32s3", "esp32:esp32:esp32s3"))
DEFAULT_TOOLCHAIN = Path("/tmp/ff-arduino-ide")
MIN_FREE_BYTES = 10 * 1024**3

# spec/device-protocol.md -> up/announce: ota_slot_size.
OTA_SLOT_SIZE = 1966080
APP_OFFSET = 0x10000
BOOT_APP0_OFFSET = 0xE000
# Fleetforge.cpp's first ESP_LOGI (tag ff-lib). Present in the app only if LOG_LOCAL_LEVEL
# reached the library's compile: the core's CONFIG_LOG_MAXIMUM_LEVEL compiles it out.
FF_LIB_LOG_NEEDLE = b"fleetforge library %s, firmware %s"

# Runbook: the Xtensa boards need none of these. Globs tolerate absence.
PRUNE_GLOBS = (
    "tools/esp-rv32",
    "tools/riscv32-esp-elf-gdb",
    "tools/esp32c3-libs",
    "tools/esp32c5-libs",
    "tools/esp32c6-libs",
    "tools/esp32h2-libs",
    "tools/esp32p4-libs",
    "tools/esp32p4_es-libs",
    "tools/esp32s2-libs",
)
NM_NAMES = ("xtensa-{target}-elf-nm", "xtensa-esp-elf-nm", "xtensa-esp32-elf-nm")

INSTALL_TIMEOUT = 60 * 60
COMPILE_TIMEOUT = 30 * 60
SHORT_TIMEOUT = 10 * 60


class CheckError(Exception):
    """A check failed: the package does not do what a persona needs."""


class SetupError(CheckError):
    """The run could not start (disk, download); nothing was proved either way."""


@dataclass
class Run:
    toolchain: Path
    work: Path
    results: list[tuple[str, str]] = field(default_factory=list)
    observations: list[tuple[str, str]] = field(default_factory=list)

    @property
    def config(self) -> Path:
        return self.toolchain / "arduino-cli.yaml"

    @property
    def cli(self) -> Path:
        return self.toolchain / "bin" / "arduino-cli"

    @property
    def libraries(self) -> Path:
        return self.toolchain / "user" / "libraries"

    def say(self, line: str = "") -> None:
        print(line, flush=True)

    def passed(self, check: str, observed: str) -> None:
        self.results.append((check, observed))
        self.say(f"  PASS  {check}: {observed}")

    def observed(self, what: str, value: str) -> None:
        self.observations.append((what, value))
        self.say(f"  NOTE  {what}: {value}")


def _load_tool(name: str) -> ModuleType:
    """agent/tools/* by path (not a package; lib_bundle imports make_manifest by bare name)."""
    if name in sys.modules:
        return sys.modules[name]
    spec = importlib.util.spec_from_file_location(name, TOOLS_DIR / f"{name}.py")
    if spec is None or spec.loader is None:
        raise SetupError(f"cannot load agent/tools/{name}.py")
    module = importlib.util.module_from_spec(spec)
    sys.modules[name] = module
    spec.loader.exec_module(module)
    return module


def df(run: Run, when: str) -> int:
    usage = shutil.disk_usage("/")
    out = subprocess.run(["df", "-h", "/"], capture_output=True, text=True, check=False).stdout
    run.say(f"  df -h / ({when}):\n" + "\n".join(f"    {line}" for line in out.splitlines()))
    return usage.free


def sh(argv: Sequence[str | Path], timeout: int) -> tuple[int, str]:
    proc = subprocess.run(
        [str(arg) for arg in argv],
        capture_output=True,
        text=True,
        timeout=timeout,
        check=False,
    )
    return proc.returncode, proc.stdout + proc.stderr


def acli(run: Run, *args: str | Path, timeout: int = SHORT_TIMEOUT) -> str:
    code, out = sh([run.cli, "--config-file", run.config, *args], timeout)
    if code != 0:
        raise CheckError(f"arduino-cli {' '.join(map(str, args))} exited {code}:\n{out[-3000:]}")
    return out


# ── toolchain ─────────────────────────────────────────────────────────────────────────────


def install_cli(run: Run) -> None:
    if run.cli.is_file():
        run.passed("arduino-cli", f"{CLI_VERSION} already in {run.cli.parent} (--keep-toolchain)")
        return
    run.toolchain.mkdir(parents=True, exist_ok=True)
    tarball = run.toolchain / f"arduino-cli_{CLI_VERSION}.tar.gz"
    with urllib.request.urlopen(CLI_URL, timeout=120) as response, tarball.open("wb") as fh:
        shutil.copyfileobj(response, fh)
    digest = hashlib.sha256(tarball.read_bytes()).hexdigest()
    if digest != CLI_SHA256:
        tarball.unlink()
        raise SetupError(f"{CLI_URL}: sha256 {digest}, pinned {CLI_SHA256}; refusing to run it")
    bin_dir = run.toolchain / "bin"
    bin_dir.mkdir()
    with tarfile.open(tarball) as archive:
        member = archive.getmember("arduino-cli")
        archive.extractall(bin_dir, members=[member], filter="data")
    tarball.unlink()
    run.cli.chmod(0o755)
    version = acli(run, "version")
    if CLI_VERSION not in version:
        raise SetupError(f"downloaded arduino-cli says {version.strip()!r}, pinned {CLI_VERSION}")
    run.passed("arduino-cli", f"{CLI_VERSION}, sha256 {CLI_SHA256[:8]}…{CLI_SHA256[-4:]} verified")


def write_config(run: Run) -> None:
    tc = run.toolchain
    run.config.write_text(
        "directories:\n"
        f"  data: {tc}/data\n"
        f"  downloads: {tc}/dl\n"
        f"  user: {tc}/user\n"
        "build_cache:\n"
        f"  path: {tc}/cache\n"
        "board_manager:\n"
        "  additional_urls:\n"
        f"    - {ESPRESSIF_INDEX}\n"
        "library:\n"
        "  enable_unsafe_install: true\n"
        "updater:\n"
        "  enable_notification: false\n"
    )


def installed_core(run: Run) -> str | None:
    loaded: Any = json.loads(acli(run, "core", "list", "--format", "json") or "{}")
    platforms = loaded.get("platforms", []) if isinstance(loaded, dict) else loaded
    for platform in platforms or []:
        if isinstance(platform, dict) and platform.get("id") == CORE:
            version = platform.get("installed_version") or platform.get("installed")
            return str(version) if version else None
    return None


def install_core(run: Run) -> None:
    current = installed_core(run)
    if current == CORE_VERSION:
        run.passed("core", f"{CORE}@{CORE_VERSION} already installed (--keep-toolchain)")
        return
    if shutil.disk_usage("/").free < MIN_FREE_BYTES:
        raise SetupError(f"/ has under {MIN_FREE_BYTES // 1024**3} GB free; the core needs ~8 GB")
    acli(run, "core", "update-index", timeout=SHORT_TIMEOUT)
    started = time.monotonic()
    acli(run, "core", "install", f"{CORE}@{CORE_VERSION}", timeout=INSTALL_TIMEOUT)
    took = time.monotonic() - started
    if installed_core(run) != CORE_VERSION:
        raise CheckError(f"core list does not show {CORE} {CORE_VERSION} after the install")
    shutil.rmtree(run.toolchain / "dl", ignore_errors=True)
    package = run.toolchain / "data" / "packages" / "esp32"
    pruned = []
    for pattern in PRUNE_GLOBS:
        for path in package.glob(pattern):
            shutil.rmtree(path, ignore_errors=True)
            pruned.append(path.name)
    run.passed("core", f"{CORE}@{CORE_VERSION} installed in {took:.0f} s")
    run.observed("pruned", ", ".join(sorted(pruned)) or "nothing")


def find_nm(run: Run, target: str) -> Path:
    tools = run.toolchain / "data" / "packages" / "esp32" / "tools"
    for name in NM_NAMES:
        found = sorted(tools.glob(f"**/bin/{name.format(target=target)}"))
        if found:
            return found[0]
    raise CheckError(f"no xtensa nm under {tools}")


# ── the package ───────────────────────────────────────────────────────────────────────────


def strip_preludes(package: Path) -> None:
    """Negative control: the package as it would be WITHOUT the LOG_LOCAL_LEVEL prelude."""
    for path in (package / "src").iterdir():
        if path.suffix in arduino_package.SOURCE_SUFFIXES:
            body = path.read_bytes()
            _, sep, rest = body.partition(b"#line 1\n")
            if not sep:
                raise CheckError(f"{path} has no prelude to strip")
            path.write_bytes(rest)


def install_package(run: Run, negative_control: bool) -> tuple[str, Path]:
    manifest = arduino_package.read_manifest(arduino_package.COMPONENT_DIR)
    version = str(manifest["version"])
    staging = run.work / "package"
    package = arduino_package.build_package(arduino_package.COMPONENT_DIR, staging)
    if negative_control:
        strip_preludes(package)
        run.observed("negative control", "the .c/.cpp files carry NO prelude")
    archive = arduino_package.make_zip(package, staging / f"Fleetforge-{version}.zip")
    shutil.rmtree(run.libraries / "Fleetforge", ignore_errors=True)
    acli(run, "lib", "install", "--zip-path", archive)
    loaded: Any = json.loads(acli(run, "lib", "list", "--format", "json") or "{}")
    entries = loaded.get("installed_libraries", []) if isinstance(loaded, dict) else loaded
    for entry in entries or []:
        library = entry.get("library", entry) if isinstance(entry, dict) else {}
        if library.get("name") == "Fleetforge":
            install_dir = Path(str(library.get("install_dir", "")))
            if library.get("version") != version:
                raise CheckError(f"lib list: Fleetforge {library.get('version')}, want {version}")
            if install_dir.resolve() != (run.libraries / "Fleetforge").resolve():
                raise CheckError(f"lib list: Fleetforge installed at {install_dir}")
            run.passed("zip install", f"lib list: Fleetforge {version} at {install_dir}")
            return version, install_dir
    raise CheckError("lib list does not show Fleetforge after lib install --zip-path")


# ── one board ─────────────────────────────────────────────────────────────────────────────


def our_warnings(output: str, sketch: Path) -> list[str]:
    ours = re.compile(
        rf"libraries/Fleetforge/|{re.escape(str(sketch))}|\bBasic\.ino\b"
        r"|\b(?:Fleetforge|ff_[a-z0-9_]+)\.(?:c|cpp|h)\b"
    )
    return [line for line in output.splitlines() if "warning:" in line and ours.search(line)]


def flash_offsets(flash_args: Path) -> dict[str, int]:
    offsets: dict[str, int] = {}
    for line in flash_args.read_text().splitlines():
        match = re.fullmatch(r"\s*(0x[0-9a-fA-F]+)\s+(\S+)\s*", line)
        if match:
            offsets[Path(match.group(2)).name] = int(match.group(1), 16)
    return offsets


def check_board(run: Run, target: str, fqbn: str, version: str, install_dir: Path) -> None:
    run.say(f"\n== {fqbn} ==")
    sketch = run.work / "sketches" / target / "Basic"
    shutil.copytree(install_dir / "examples" / "Basic", sketch)
    build = run.work / f"build-{target}"
    started = time.monotonic()
    # JSON, so the "Used library" table is data (arduino-cli 1.5 prints it only with -v).
    argv = [run.cli, "--config-file", run.config, "compile", "-b", fqbn, "--warnings", "all"]
    argv += ["--build-path", build, "--format", "json", sketch]
    proc = subprocess.run(
        [str(arg) for arg in argv],
        capture_output=True,
        text=True,
        timeout=COMPILE_TIMEOUT,
        check=False,
    )
    took = time.monotonic() - started
    try:
        result: Any = json.loads(proc.stdout)
    except json.JSONDecodeError:
        result = {}
    if not isinstance(result, dict):
        result = {}
    out = f"{result.get('compiler_out', '')}{result.get('compiler_err', '')}{proc.stderr}"
    # a. exit 0
    if proc.returncode != 0 or result.get("success") is not True:
        detail = out or proc.stdout
        raise CheckError(f"{fqbn}: compile exited {proc.returncode}:\n{detail[-4000:]}")
    run.passed(f"{target} compile", f"exit 0 in {took:.0f} s")
    # b. no warnings of ours
    found = our_warnings(out, sketch)
    if found:
        raise CheckError(
            f"{fqbn}: {len(found)} warning(s) from the library/sketch:\n" + "\n".join(found[:20])
        )
    total = sum(1 for line in out.splitlines() if "warning:" in line)
    run.passed(f"{target} warnings from Fleetforge or the sketch", f"0 (all warnings: {total})")
    # c. the installed copy is the one used
    builder = result.get("builder_result") or {}
    used = [lib for lib in builder.get("used_libraries") or [] if lib.get("name") == "Fleetforge"]
    if len(used) != 1 or used[0].get("version") != version:
        raise CheckError(f"{fqbn}: used libraries {used}, want one Fleetforge {version}")
    used_dir = Path(str(used[0].get("install_dir", "")))
    if used_dir.resolve() != install_dir.resolve():
        raise CheckError(f"{fqbn}: Fleetforge used from {used_dir}, not {install_dir}")
    run.passed(f"{target} used library", f"Fleetforge {version} {used_dir}")
    # d. app size
    app = build / "Basic.ino.bin"
    size = app.stat().st_size if app.is_file() else 0
    if not 0 < size < OTA_SLOT_SIZE:
        raise CheckError(f"{fqbn}: Basic.ino.bin is {size} B, slot {OTA_SLOT_SIZE}")
    run.passed(
        f"{target} app size", f"{size} B < {OTA_SLOT_SIZE} ({100 * size / OTA_SLOT_SIZE:.1f}%)"
    )
    # e. the built table is ab-4m-arduino-v1
    make_manifest = _load_tool("make_manifest")
    lib_bundle = _load_tool("lib_bundle")
    rows = make_manifest.decode_partition_table(build / "Basic.ino.partitions.bin")
    fingerprint = str(lib_bundle.layout_fingerprint(rows))
    if fingerprint != lib_bundle.ARDUINO_LAYOUT_SHA256:
        raise CheckError(
            f"{fqbn}: built partitions.bin fingerprints to {fingerprint}, "
            f"not {lib_bundle.ARDUINO_LAYOUT_ID} ({lib_bundle.ARDUINO_LAYOUT_SHA256})"
        )
    run.passed(
        f"{target} partitions.bin",
        f"{fingerprint[:8]}…{fingerprint[-4:]} = {lib_bundle.ARDUINO_LAYOUT_ID}",
    )
    # f. the upload writes where the table says
    offsets = flash_offsets(build / "flash_args")
    if (
        offsets.get("Basic.ino.bin") != APP_OFFSET
        or offsets.get("boot_app0.bin") != BOOT_APP0_OFFSET
    ):
        raise CheckError(f"{fqbn}: flash_args offsets {offsets}")
    run.passed(f"{target} flash_args", "app @ 0x10000, boot_app0 @ 0xe000")
    # g. the strong verifyRollbackLater
    nm = find_nm(run, target)
    code, symbols = sh([nm, build / "Basic.ino.elf"], SHORT_TIMEOUT)
    if code != 0:
        raise CheckError(f"{nm.name} exited {code}: {symbols[-1000:]}")
    line = next((s for s in symbols.splitlines() if s.endswith(" verifyRollbackLater")), "")
    if " T verifyRollbackLater" not in line:
        raise CheckError(f"{fqbn}: verifyRollbackLater is {line.strip() or 'absent'}, want T")
    run.passed(f"{target} verifyRollbackLater", line.strip())
    # h. the ff-lib INFO line survived (LOG_LOCAL_LEVEL prelude)
    if FF_LIB_LOG_NEEDLE not in app.read_bytes():
        raise CheckError(
            f"{fqbn}: the app has no {FF_LIB_LOG_NEEDLE.decode()!r}: LOG_LOCAL_LEVEL did not "
            "reach the library's compile, every ESP_LOGI of it is compiled out"
        )
    run.passed(f"{target} ff-lib INFO string", FF_LIB_LOG_NEEDLE.decode())
    # i. observation only
    maximum = re.search(r"^Sketch uses .*$", out, re.M)
    run.observed(f"{target} size line", maximum.group(0) if maximum else "absent")


# ── main ──────────────────────────────────────────────────────────────────────────────────


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0] if __doc__ else None)
    parser.add_argument("--toolchain-dir", type=Path, default=DEFAULT_TOOLCHAIN)
    parser.add_argument(
        "--keep-toolchain",
        action="store_true",
        help="leave the scratch toolchain for a rerun (default: delete it at the end)",
    )
    parser.add_argument(
        "--negative-control",
        action="store_true",
        help="install a package with the prelude stripped; check (h) must fail",
    )
    args = parser.parse_args(argv)
    if args.toolchain_dir.resolve() in (Path.home().resolve(), Path("/")):
        print(f"refusing toolchain dir {args.toolchain_dir}", file=sys.stderr)
        return 2
    run = Run(
        toolchain=args.toolchain_dir.resolve(),
        work=Path(tempfile.mkdtemp(prefix="ff-arduino-ide-work-")),
    )
    started = time.monotonic()
    code = 1
    try:
        run.say(f"== toolchain in {run.toolchain} (work {run.work}) ==")
        df(run, "before")
        install_cli(run)
        write_config(run)
        install_core(run)
        df(run, "toolchain installed")
        run.say("\n== package ==")
        version, install_dir = install_package(run, args.negative_control)
        for target, fqbn in BOARDS:
            check_board(run, target, fqbn, version, install_dir)
        code = 0
    except SetupError as error:
        run.say(f"\nSETUP FAILED: {error}")
        code = 2
    except (CheckError, arduino_package.PackageError) as error:
        run.say(f"\nFAILED: {error}")
    except (subprocess.SubprocessError, OSError, ValueError, KeyError) as error:
        run.say(f"\nFAILED: {type(error).__name__}: {error}")
    finally:
        shutil.rmtree(run.work, ignore_errors=True)
        if args.keep_toolchain:
            run.say(f"\nKEPT {run.toolchain} (rm -rf it when done)")
        else:
            shutil.rmtree(run.toolchain, ignore_errors=True)
            run.say(f"\nremoved {run.toolchain}")
        df(run, "after")

    run.say(f"\n== {'PASS' if code == 0 else 'FAIL'} ({time.monotonic() - started:.0f} s) ==")
    rows = run.results + [(f"(note) {what}", value) for what, value in run.observations]
    if rows:
        width = max(len(check) for check, _ in rows)
        for check, observed in rows:
            print(f"  {check.ljust(width)}  {observed}")
    return code


if __name__ == "__main__":
    sys.exit(main())

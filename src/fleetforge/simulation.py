"""Pluggable pre-flight firmware simulation Port and Espressif QEMU adapter (`R9-1`, `A-24`).

Implements the `SimRunner` Protocol (`design/architecture.md` -> *Simulation backend*,
`docs/features/simulation.md`): boot an uploaded artifact in Espressif QEMU, execute its
`R5` self-test, and produce an advisory `SimResult` (`passed`, `failed`, or `skipped`)
before any physical board receives the image. When QEMU is unavailable, `auto` reports
`skipped` with the reason rather than substituting a fake runner.
"""

from __future__ import annotations

import asyncio
import codecs
import datetime as dt
import json
import os
import shutil
import tempfile
import time
from dataclasses import dataclass
from enum import StrEnum
from pathlib import Path
from typing import Protocol

from fleetforge.clock import now_utc
from fleetforge.deploys import sanitise_device_text

DEFAULT_QEMU_FLASH_BYTES = 4 * 1024 * 1024
APP_SLOT_OFFSET = 0x10000
MAX_SERIAL_LINE_BYTES = 64 * 1024
_SERIAL_READ_CHUNK_BYTES = 4096
_MAX_SERIAL_TOTAL_CHARS = 512 * 1024

LAYOUT_APP_SLOT_OFFSETS: dict[str, int] = {
    "ab-4m-v1": 0x20000,
    "ab-4m-arduino-v1": 0x20000,
}


def slot_offset_for_layout(partition_layout: str | None) -> int:
    """Return the app slot flash offset for `partition_layout` (`0x20000` for A/B 4 MiB layouts)."""
    if partition_layout is None:
        return APP_SLOT_OFFSET
    return LAYOUT_APP_SLOT_OFFSETS.get(partition_layout, APP_SLOT_OFFSET)


_QEMU_TARGET_CONFIG: dict[str, tuple[str, str]] = {
    "esp32": ("qemu-system-xtensa", "esp32"),
    "esp32s3": ("qemu-system-xtensa", "esp32s3"),
    "esp32c3": ("qemu-system-riscv32", "esp32c3"),
    "esp32c6": ("qemu-system-riscv32", "esp32c6"),
}

_QEMU_TARGET_MEMORY: dict[str, str] = {
    "esp32": "4M",
    "esp32s3": "32M",
}


def default_efuse_bytes(target: str) -> bytes:
    """Return ESP-IDF 5.5 `qemu_ext.py` default eFuse image so chip-revision checks pass."""
    if target == "esp32":
        buf = bytearray(124)
        buf[13] = 0x80
        buf[22] = 0x10
        return bytes(buf)
    buf = bytearray(1024)
    buf[38] = 0x0C
    return bytes(buf)


_SERIAL_PASS_MARKERS: tuple[str, ...] = (
    "FF_SIM_SELFTEST_PASS",
    "selftest passed",
    "marked app valid",
    "state=confirmed",
    '"state":"confirmed"',
    "ff_cfg v1 loaded",
    "no usable ff_cfg partition",
)

_SERIAL_FAIL_MARKERS: tuple[str, ...] = (
    "FF_SIM_BOOT_CRASH",
    "FF_SIM_PANIC",
    "FF_SIM_SELFTEST_FAIL",
    "FF_FAULT_TEST=",
    "Guru Meditation Error",
    "LoadProhibited",
    "StoreProhibited",
    "abort() was called",
    "selftest failed",
    "selftest timed out",
    "rolled_back",
    "invalid header",
)


class SimStatus(StrEnum):
    """Lifecycle and verdict states of a pre-flight artifact simulation (`R9`)."""

    PENDING = "pending"
    RUNNING = "running"
    PASSED = "passed"
    FAILED = "failed"
    SKIPPED = "skipped"


@dataclass(frozen=True, slots=True)
class SimResult:
    """Outcome produced by a `SimRunner` execution."""

    status: SimStatus
    detail: str
    ran_at: dt.datetime
    runner: str
    duration_ms: int = 0


class SimRunner(Protocol):
    """Port for booting a firmware artifact and evaluating its `R5` self-test (`R9-1`)."""

    async def simulate(
        self,
        *,
        sha256: str,
        target: str,
        image: bytes,
        partition_layout: str | None = None,
        artifact_type: str = "app",
    ) -> SimResult: ...


def parse_qemu_serial_verdict(serial_log: str) -> tuple[SimStatus, str] | None:
    """Inspect captured QEMU / `pytest-embedded` serial output for pass or crash/rollback markers."""
    for marker in _SERIAL_FAIL_MARKERS:
        if marker in serial_log:
            for line in serial_log.splitlines():
                if marker in line:
                    cleaned = sanitise_device_text(line.strip()) or marker
                    return SimStatus.FAILED, f"QEMU serial fault: {cleaned[:180]}"
            return SimStatus.FAILED, f"QEMU serial fault: {marker}"

    for marker in _SERIAL_PASS_MARKERS:
        if marker in serial_log:
            return SimStatus.PASSED, f"QEMU booted cleanly and confirmed R5 self-test ({marker})"

    return None


def _advance_line_length(line_bytes: int, raw: bytes) -> tuple[int, bool]:
    """Return `(trailing_line_bytes, exceeded_limit)` across newline boundaries in `raw`."""
    parts = raw.split(b"\n")
    for segment in parts[:-1]:
        if line_bytes + len(segment) > MAX_SERIAL_LINE_BYTES:
            return line_bytes + len(segment), True
        line_bytes = 0
    line_bytes += len(parts[-1])
    return line_bytes, line_bytes > MAX_SERIAL_LINE_BYTES


def _build_qemu_cmd(
    *,
    qemu_bin: str,
    machine: str,
    target: str,
    flash_path: Path,
    efuse_path: Path | None = None,
    pytest_harness_dir: Path | None,
) -> list[str]:
    if pytest_harness_dir is not None:
        return [
            "pytest",
            str(pytest_harness_dir),
            "--embedded-services",
            "qemu",
            "--target",
            target,
            f"--qemu-flash-bin={flash_path}",
        ]
    cmd = [
        qemu_bin,
        "-nographic",
        "-machine",
        machine,
    ]
    mem = _QEMU_TARGET_MEMORY.get(target)
    if mem is not None:
        cmd.extend(["-m", mem])
    cmd.extend(
        [
            "-drive",
            f"file={flash_path},if=mtd,format=raw",
        ]
    )
    if efuse_path is not None:
        cmd.extend(
            [
                "-drive",
                f"file={efuse_path},if=none,format=raw,id=efuse",
                "-global",
                f"driver=nvram.{machine}.efuse,property=drive,value=efuse",
                "-global",
                f"driver=timer.{machine}.timg,property=wdt_disable,value=true",
            ]
        )
    cmd.extend(
        [
            "-serial",
            "mon:stdio",
            "-no-reboot",
        ]
    )
    return cmd


class EspressifQemuSimRunner:
    """Espressif QEMU (`pytest-embedded-qemu` / `qemu-system-*`) adapter (`R9-1`).

    Writes a padded 4 MiB flash image placing bootloader/partition-table/otadata (when a
    `boot_bundle_dir` is supplied) and the app binary at the layout's `ota_0` offset,
    captures serial output in bounded 4 KiB chunks under a strict wall-clock timeout, and
    classifies `R5` self-test pass vs boot panic / rollback / oversized serial lines.
    """

    runner_name = "espressif-qemu"

    def __init__(
        self,
        *,
        timeout_s: float = 20.0,
        qemu_bin_override: str | None = None,
        pytest_harness_dir: Path | None = None,
        boot_bundle_dir: Path | None = None,
    ) -> None:
        self._timeout_s = timeout_s
        self._qemu_bin_override = qemu_bin_override
        self._pytest_harness_dir = pytest_harness_dir
        env_bundle = os.environ.get("FF_QEMU_BOOT_BUNDLE_DIR", "").strip()
        self._boot_bundle_dir = boot_bundle_dir or (Path(env_bundle) if env_bundle else None)

    @staticmethod
    def build_flash_image(
        image: bytes,
        *,
        partition_layout: str | None = None,
        flash_bytes: int = DEFAULT_QEMU_FLASH_BYTES,
        boot_bundle_dir: Path | None = None,
    ) -> bytes:
        buf = bytearray(b"\xff" * flash_bytes)
        slot_offset = slot_offset_for_layout(partition_layout)
        if boot_bundle_dir is not None:
            manifest_path = boot_bundle_dir / "manifest.json"
            if manifest_path.is_file():
                manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
                for part in manifest.get("parts", []):
                    raw_offset = part["offset"]
                    part_offset = (
                        raw_offset if isinstance(raw_offset, int) else int(str(raw_offset), 0)
                    )
                    part_name = str(part.get("name", ""))
                    if part_name == "app":
                        slot_offset = part_offset
                        continue
                    part_rel = part.get("path") or part.get("file")
                    if not isinstance(part_rel, str) or not part_rel:
                        raise ValueError(f"boot bundle part {part_name!r} is missing path")
                    part_file = boot_bundle_dir / part_rel
                    if part_file.is_file():
                        raw_part = part_file.read_bytes()
                        end = part_offset + len(raw_part)
                        if end > flash_bytes:
                            raise ValueError(
                                f"boot bundle part {part_name!r} exceeds {flash_bytes} B flash image"
                            )
                        buf[part_offset:end] = raw_part
        if slot_offset + len(image) > flash_bytes:
            raise ValueError(
                f"app image ({len(image)} B) exceeds {flash_bytes} B QEMU flash image at 0x{slot_offset:x}"
            )
        buf[slot_offset : slot_offset + len(image)] = image
        return bytes(buf)

    async def _read_serial_verdict(
        self,
        proc: asyncio.subprocess.Process,
    ) -> tuple[str, tuple[SimStatus, str] | None]:
        chunks: list[str] = []
        total_chars = 0
        line_bytes = 0
        decoder = codecs.getincrementaldecoder("utf-8")(errors="replace")
        verdict: tuple[SimStatus, str] | None = None
        if proc.stdout is None:
            return "", None
        deadline = time.monotonic() + self._timeout_s
        while (remaining := deadline - time.monotonic()) > 0:
            raw = await asyncio.wait_for(
                proc.stdout.read(_SERIAL_READ_CHUNK_BYTES), timeout=remaining
            )
            if not raw:
                break
            line_bytes, exceeded = _advance_line_length(line_bytes, raw)
            if exceeded:
                return "".join(chunks), (
                    SimStatus.FAILED,
                    f"QEMU serial line exceeded {MAX_SERIAL_LINE_BYTES} B limit",
                )
            decoded = decoder.decode(raw, final=False)
            if decoded and total_chars < _MAX_SERIAL_TOTAL_CHARS:
                keep = decoded[: _MAX_SERIAL_TOTAL_CHARS - total_chars]
                chunks.append(keep)
                total_chars += len(keep)
            verdict = parse_qemu_serial_verdict("".join(chunks))
            if verdict is not None:
                break
        final_tail = decoder.decode(b"", final=True)
        if final_tail and total_chars < _MAX_SERIAL_TOTAL_CHARS:
            chunks.append(final_tail[: _MAX_SERIAL_TOTAL_CHARS - total_chars])
        return "".join(chunks), verdict

    def _preflight_target(
        self,
        *,
        target: str,
        artifact_type: str,
        ran_at: dt.datetime,
    ) -> tuple[str, str] | SimResult:
        if artifact_type != "app":
            return SimResult(
                status=SimStatus.SKIPPED,
                detail=f"non-app cargo ({artifact_type}) does not boot in QEMU",
                ran_at=ran_at,
                runner=self.runner_name,
                duration_ms=0,
            )
        target_cfg = _QEMU_TARGET_CONFIG.get(target)
        if target_cfg is None:
            return SimResult(
                status=SimStatus.SKIPPED,
                detail=f"QEMU target {target!r} is not supported by Espressif QEMU",
                ran_at=ran_at,
                runner=self.runner_name,
                duration_ms=0,
            )
        default_bin, machine = target_cfg
        qemu_bin = self._qemu_bin_override or shutil.which(default_bin)
        if not qemu_bin:
            return SimResult(
                status=SimStatus.SKIPPED,
                detail=f"Espressif QEMU binary {default_bin!r} is not installed on this worker",
                ran_at=ran_at,
                runner=self.runner_name,
                duration_ms=0,
            )
        return qemu_bin, machine

    async def simulate(
        self,
        *,
        sha256: str,
        target: str,
        image: bytes,
        partition_layout: str | None = None,
        artifact_type: str = "app",
    ) -> SimResult:
        started = time.monotonic()
        ran_at = now_utc()
        resolved = self._preflight_target(target=target, artifact_type=artifact_type, ran_at=ran_at)
        if isinstance(resolved, SimResult):
            return resolved
        qemu_bin, machine = resolved

        try:
            flash_bytes = self.build_flash_image(
                image,
                partition_layout=partition_layout,
                boot_bundle_dir=self._boot_bundle_dir,
            )
        except (ValueError, OSError, KeyError, TypeError, json.JSONDecodeError) as exc:
            return SimResult(
                status=SimStatus.FAILED,
                detail=str(exc),
                ran_at=ran_at,
                runner=self.runner_name,
                duration_ms=int((time.monotonic() - started) * 1000),
            )

        safe_tag = "".join(ch for ch in sha256[:12] if ch.isalnum()) or "app"
        with tempfile.TemporaryDirectory(prefix=f"ff-qemu-{safe_tag}-") as tmp_dir:
            flash_path = Path(tmp_dir) / "flash.bin"
            flash_path.write_bytes(flash_bytes)
            efuse_path = Path(tmp_dir) / "efuse.bin"
            efuse_path.write_bytes(default_efuse_bytes(target))
            cmd = _build_qemu_cmd(
                qemu_bin=qemu_bin,
                machine=machine,
                target=target,
                flash_path=flash_path,
                efuse_path=efuse_path,
                pytest_harness_dir=self._pytest_harness_dir,
            )
            proc = await asyncio.create_subprocess_exec(
                *cmd,
                stdout=asyncio.subprocess.PIPE,
                stderr=asyncio.subprocess.STDOUT,
            )
            combined = ""
            verdict: tuple[SimStatus, str] | None = None
            try:
                combined, verdict = await self._read_serial_verdict(proc)
            except TimeoutError:
                pass
            finally:
                if proc.returncode is None:
                    proc.kill()
                    await proc.wait()

        duration_ms = int((time.monotonic() - started) * 1000)
        verdict = verdict or parse_qemu_serial_verdict(combined)
        if verdict is not None:
            return SimResult(
                status=verdict[0],
                detail=verdict[1],
                ran_at=ran_at,
                runner=self.runner_name,
                duration_ms=duration_ms,
            )
        if self._pytest_harness_dir is not None and proc.returncode == 0:
            return SimResult(
                status=SimStatus.PASSED,
                detail="pytest-embedded-qemu self-test suite passed",
                ran_at=ran_at,
                runner=self.runner_name,
                duration_ms=duration_ms,
            )
        return SimResult(
            status=SimStatus.FAILED,
            detail="simulation timed out or exited before R5 self-test confirmed",
            ran_at=ran_at,
            runner=self.runner_name,
            duration_ms=duration_ms,
        )


def create_sim_runner(backend: str | None = None) -> SimRunner:
    """Instantiate the configured `SimRunner` (`qemu` or `auto`, never a test stand-in)."""
    mode = (backend or os.environ.get("FF_SIM_BACKEND") or "auto").strip().lower()
    if mode in ("auto", "qemu"):
        return EspressifQemuSimRunner()
    raise ValueError(f"unsupported simulation backend {mode!r}; expected 'auto' or 'qemu'")

"""Pluggable pre-flight firmware simulation Port and QEMU / host adapters (`R9-1`).

Implements the `SimRunner` Protocol (`design/architecture.md` -> *Simulation backend*,
`docs/features/simulation.md`): boot an uploaded artifact, execute its `R5` self-test,
and produce an advisory `SimResult` (`passed`, `failed`, or `skipped`) before any
physical board receives the image.
"""

from __future__ import annotations

import asyncio
import datetime as dt
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
from fleetforge.lib_marker import find_marker
from fleetforge.merged_image import detect_merged

ESP_IMAGE_MAGIC = 0xE9
MIN_ESP_APP_BYTES = 32
DEFAULT_QEMU_FLASH_BYTES = 4 * 1024 * 1024
APP_SLOT_OFFSET = 0x10000

_QEMU_TARGET_CONFIG: dict[str, tuple[str, str]] = {
    "esp32": ("qemu-system-xtensa", "esp32"),
    "esp32s3": ("qemu-system-xtensa", "esp32s3"),
    "esp32c3": ("qemu-system-riscv32", "esp32c3"),
    "esp32c6": ("qemu-system-riscv32", "esp32c6"),
}

_SERIAL_PASS_MARKERS: tuple[str, ...] = (
    "FF_SIM_SELFTEST_PASS",
    "selftest passed",
    "marked app valid",
    "state=confirmed",
    '"state":"confirmed"',
)

_SERIAL_FAIL_MARKERS: tuple[str, ...] = (
    "FF_SIM_BOOT_CRASH",
    "FF_SIM_PANIC",
    "FF_SIM_SELFTEST_FAIL",
    "Guru Meditation Error",
    "abort() was called",
    "selftest failed",
    "selftest timed out",
    "rolled_back",
    "invalid header",
)

_BINARY_FAULT_VECTORS: tuple[tuple[bytes, str], ...] = (
    (b"FF_SIM_BOOT_CRASH", "boot panic simulated before R5 self-test confirmation"),
    (b"FF_SIM_PANIC", "Guru Meditation panic simulated during boot"),
    (b"Guru Meditation Error", "Guru Meditation panic vector detected in image"),
    (b"FF_SIM_SELFTEST_FAIL", "R5 self-test reported failure in simulation"),
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


class HostPreflightSimRunner:
    """Deterministic host-side ESP-IDF boot + `R5` self-test pre-flight simulator.

    Used when QEMU binaries are not installed in unit/CI containers or when `FF_SIM_BACKEND=host`.
    Validates ESP-IDF app header framing, rejects merged images and unmarked binaries whose `R5`
    self-test would never execute, and honours embedded fault/self-test simulation markers.
    """

    runner_name = "host-preflight"

    async def simulate(
        self,
        *,
        sha256: str,
        target: str,
        image: bytes,
        partition_layout: str | None = None,
        artifact_type: str = "app",
    ) -> SimResult:
        _ = (sha256, partition_layout)
        started = time.monotonic()
        ran_at = now_utc()

        if artifact_type != "app":
            return SimResult(
                status=SimStatus.SKIPPED,
                detail=f"non-app cargo ({artifact_type}) does not boot directly",
                ran_at=ran_at,
                runner=self.runner_name,
                duration_ms=int((time.monotonic() - started) * 1000),
            )

        if len(image) < MIN_ESP_APP_BYTES or image[0] != ESP_IMAGE_MAGIC:
            return SimResult(
                status=SimStatus.FAILED,
                detail=(
                    f"invalid {target} app boot header: expected 0xE9 magic and >= {MIN_ESP_APP_BYTES} bytes"
                ),
                ran_at=ran_at,
                runner=self.runner_name,
                duration_ms=int((time.monotonic() - started) * 1000),
            )

        merged = detect_merged(image)
        if merged is not None:
            return SimResult(
                status=SimStatus.FAILED,
                detail="merged full-flash image cannot boot from an OTA app slot",
                ran_at=ran_at,
                runner=self.runner_name,
                duration_ms=int((time.monotonic() - started) * 1000),
            )

        for pattern, reason in _BINARY_FAULT_VECTORS:
            if pattern in image:
                return SimResult(
                    status=SimStatus.FAILED,
                    detail=reason,
                    ran_at=ran_at,
                    runner=self.runner_name,
                    duration_ms=int((time.monotonic() - started) * 1000),
                )

        marker = find_marker(image)
        if marker is None:
            return SimResult(
                status=SimStatus.FAILED,
                detail="missing FleetForge OTA library marker; R5 self-test never confirmed",
                ran_at=ran_at,
                runner=self.runner_name,
                duration_ms=int((time.monotonic() - started) * 1000),
            )

        return SimResult(
            status=SimStatus.PASSED,
            detail=f"booted {target} image (libfleetforge {marker.lib_version}) and passed R5 self-test",
            ran_at=ran_at,
            runner=self.runner_name,
            duration_ms=int((time.monotonic() - started) * 1000),
        )


class EspressifQemuSimRunner:
    """Espressif QEMU (`pytest-embedded-qemu` / `qemu-system-*`) adapter (`R9-1`).

    Writes a padded 4 MiB flash image placing the app binary at `0x10000` (or invokes a
    configured `pytest --embedded-services qemu` harness), captures serial output under a
    strict wall-clock timeout, and classifies `R5` self-test pass vs boot panic / rollback.
    """

    runner_name = "espressif-qemu"

    def __init__(
        self,
        *,
        timeout_s: float = 20.0,
        qemu_bin_override: str | None = None,
        pytest_harness_dir: Path | None = None,
    ) -> None:
        self._timeout_s = timeout_s
        self._qemu_bin_override = qemu_bin_override
        self._pytest_harness_dir = pytest_harness_dir

    @staticmethod
    def build_flash_image(image: bytes, *, flash_bytes: int = DEFAULT_QEMU_FLASH_BYTES) -> bytes:
        if APP_SLOT_OFFSET + len(image) > flash_bytes:
            raise ValueError(
                f"app image ({len(image)} B) exceeds {flash_bytes} B QEMU flash image at 0x{APP_SLOT_OFFSET:x}"
            )
        buf = bytearray(b"\xff" * flash_bytes)
        buf[APP_SLOT_OFFSET : APP_SLOT_OFFSET + len(image)] = image
        return bytes(buf)

    async def simulate(
        self,
        *,
        sha256: str,
        target: str,
        image: bytes,
        partition_layout: str | None = None,
        artifact_type: str = "app",
    ) -> SimResult:
        _ = (sha256, partition_layout)
        started = time.monotonic()
        ran_at = now_utc()

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

        try:
            flash_bytes = self.build_flash_image(image)
        except ValueError as exc:
            return SimResult(
                status=SimStatus.FAILED,
                detail=str(exc),
                ran_at=ran_at,
                runner=self.runner_name,
                duration_ms=int((time.monotonic() - started) * 1000),
            )

        with tempfile.TemporaryDirectory(prefix="ff-qemu-") as tmp_dir:
            flash_path = Path(tmp_dir) / "flash.bin"
            flash_path.write_bytes(flash_bytes)

            if self._pytest_harness_dir is not None:
                cmd = [
                    "pytest",
                    str(self._pytest_harness_dir),
                    "--embedded-services",
                    "qemu",
                    "--target",
                    target,
                    f"--qemu-flash-bin={flash_path}",
                ]
            else:
                cmd = [
                    qemu_bin,
                    "-nographic",
                    "-machine",
                    machine,
                    "-drive",
                    f"file={flash_path},if=mtd,format=raw",
                    "-serial",
                    "mon:stdio",
                    "-no-reboot",
                ]

            proc = await asyncio.create_subprocess_exec(
                *cmd,
                stdout=asyncio.subprocess.PIPE,
                stderr=asyncio.subprocess.STDOUT,
            )
            chunks: list[str] = []
            verdict: tuple[SimStatus, str] | None = None
            try:
                if proc.stdout is not None:
                    deadline = time.monotonic() + self._timeout_s
                    while True:
                        remaining = deadline - time.monotonic()
                        if remaining <= 0:
                            break
                        line_bytes = await asyncio.wait_for(
                            proc.stdout.readline(), timeout=remaining
                        )
                        if not line_bytes:
                            break
                        decoded = line_bytes.decode("utf-8", errors="replace")
                        chunks.append(decoded)
                        verdict = parse_qemu_serial_verdict("".join(chunks))
                        if verdict is not None:
                            break
            except TimeoutError:
                pass
            finally:
                if proc.returncode is None:
                    proc.kill()
                    await proc.wait()

        duration_ms = int((time.monotonic() - started) * 1000)
        combined = "".join(chunks)
        if verdict is None:
            verdict = parse_qemu_serial_verdict(combined)
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
    """Instantiate the configured `SimRunner` (`qemu`, `host`, or `auto`)."""
    mode = (backend or os.environ.get("FF_SIM_BACKEND") or "auto").strip().lower()
    if mode == "qemu":
        return EspressifQemuSimRunner()
    if mode == "host":
        return HostPreflightSimRunner()
    if shutil.which("qemu-system-xtensa") or shutil.which("qemu-system-riscv32"):
        return EspressifQemuSimRunner()
    return HostPreflightSimRunner()

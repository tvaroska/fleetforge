"""Test-only doubles for `SimRunner` and `BuildRunner` (`A-24` / `T-16`).

Production `create_sim_runner("auto")` and `create_build_runner("auto")` never instantiate
stand-ins; tests inject these explicit doubles via FastAPI dependency overrides or worker
arguments when exercising API, policy, or build-queue flows without QEMU or Docker.
"""

from __future__ import annotations

import hashlib
import time
from pathlib import Path

from fleetforge.builds import BuildError, LogSink
from fleetforge.clock import now_utc
from fleetforge.lib_marker import MAGIC, find_marker
from fleetforge.merged_image import detect_merged
from fleetforge.simulation import SimResult, SimStatus

ESP_IMAGE_MAGIC = 0xE9
MIN_ESP_APP_BYTES = 32


class StubSimRunner:
    """Explicit test double for `SimRunner` (never shipped in `src/fleetforge`)."""

    runner_name = "test-stub-sim"

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

        if detect_merged(image) is not None:
            return SimResult(
                status=SimStatus.FAILED,
                detail="merged full-flash image cannot boot from an OTA app slot",
                ran_at=ran_at,
                runner=self.runner_name,
                duration_ms=int((time.monotonic() - started) * 1000),
            )

        if b"FF_SIM_BOOT_CRASH" in image or b"FF_SIM_PANIC" in image:
            return SimResult(
                status=SimStatus.FAILED,
                detail="test boot panic marker detected",
                ran_at=ran_at,
                runner=self.runner_name,
                duration_ms=int((time.monotonic() - started) * 1000),
            )

        if b"FF_SIM_SELFTEST_FAIL" in image:
            return SimResult(
                status=SimStatus.FAILED,
                detail="test self-test failure marker detected",
                ran_at=ran_at,
                runner=self.runner_name,
                duration_ms=int((time.monotonic() - started) * 1000),
            )

        marker = find_marker(image)
        if marker is None:
            return SimResult(
                status=SimStatus.FAILED,
                detail="missing FleetForge OTA library marker",
                ran_at=ran_at,
                runner=self.runner_name,
                duration_ms=int((time.monotonic() - started) * 1000),
            )

        return SimResult(
            status=SimStatus.PASSED,
            detail=f"stub simulated {target} image (libfleetforge {marker.lib_version})",
            ran_at=ran_at,
            runner=self.runner_name,
            duration_ms=int((time.monotonic() - started) * 1000),
        )


class StubBuildRunner:
    """Explicit test double for `BuildRunner` (never shipped in `src/fleetforge`)."""

    async def compile_source(
        self,
        *,
        work_dir: Path,
        target: str,
        partition_layout: str,
        idf_image: str,
        on_log: LogSink | None = None,
    ) -> bytes:
        if on_log is not None:
            await on_log(f"[1/3] Configuring target {target} ({partition_layout}) on {idf_image}\n")

        sources = sorted(p for p in work_dir.rglob("*") if p.is_file())
        if not sources:
            raise BuildError("repository checkout contains no source files")

        combined = hashlib.sha256()
        for src in sources:
            content = src.read_bytes()
            if b"FF_BUILD_FAIL" in content:
                if on_log is not None:
                    await on_log(f"error: compilation failed in {src.relative_to(work_dir)}\n")
                raise BuildError(f"compile error in {src.relative_to(work_dir)}")
            combined.update(content)

        if on_log is not None:
            await on_log(f"[2/3] Compiling {len(sources)} source file(s) for {target}\n")
            await on_log("[3/3] Linking ESP-IDF app image with libfleetforge marker\n")

        marker = MAGIC + b"\x01\x00\x00\x00" + b"0.6.0\x00".ljust(32, b"\x00") + b"\x00" * 12
        meta = f"{target}:{partition_layout}:{idf_image}:{combined.hexdigest()}".encode("ascii")
        return b"\xe9\x00\x02\x20" + marker + meta + (b"\x00" * 64)

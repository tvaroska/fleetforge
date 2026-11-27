"""Shared synthetic agent-bundle test builders for catalog, publish, and `/v1/agent` suites (`T-11`)."""

from __future__ import annotations

import hashlib
import json
import logging
from pathlib import Path
from typing import Any

from fleetforge.firmware import EXPECTED_OTA_SLOT_SIZE, EXPECTED_PARTITION_LAYOUT

PART_OFFSETS = {
    "esp32": {"bootloader": 4096, "partition-table": 32768, "ota-data": 61440, "app": 131072},
    "esp32c6": {"bootloader": 0, "partition-table": 32768, "ota-data": 61440, "app": 131072},
}
PART_FILES = {
    "bootloader": "bootloader.bin",
    "partition-table": "partition-table.bin",
    "ota-data": "ota-data-initial.bin",
    "app": "app.bin",
}


def write_bundle(
    root: Path,
    target: str = "esp32",
    *,
    manifest_overrides: dict[str, Any] | None = None,
    part_overrides: dict[str, dict[str, Any]] | None = None,
    skip_files: tuple[str, ...] = (),
    dir_name: str | None = None,
) -> Path:
    """Write a synthetic but structurally faithful bundle to `root/<target>`."""
    bundle_dir = root / (dir_name or target)
    bundle_dir.mkdir(parents=True, exist_ok=True)

    parts: list[dict[str, Any]] = []
    for name, filename in PART_FILES.items():
        payload = f"{target}-{name}-payload".encode() * 8
        if filename not in skip_files:
            (bundle_dir / filename).write_bytes(payload)
        entry: dict[str, Any] = {
            "name": name,
            "path": filename,
            "offset": PART_OFFSETS.get(target, PART_OFFSETS["esp32"])[name],
            "size": len(payload),
            "sha256": hashlib.sha256(payload).hexdigest(),
        }
        entry.update((part_overrides or {}).get(name, {}))
        parts.append(entry)

    manifest: dict[str, Any] = {
        "schema": 1,
        "target": target,
        "chip_family": "ESP32-C6" if target == "esp32c6" else "ESP32",
        "agent_version": "0.1.0",
        "idf_version": "v5.5.5",
        "idf_image": "espressif/idf:v5.5.5@sha256:" + "a" * 64,
        "source_commit": "0" * 40,
        "built_at": "2026-09-09T12:00:00Z",
        "partition_layout": EXPECTED_PARTITION_LAYOUT,
        "ota_slot_size": EXPECTED_OTA_SLOT_SIZE,
        "flash_size": "4MB",
        "config_sha256": "c0" * 32,
        "build_digest": "b1" * 32,
        "config_partition": {"label": "ff_cfg", "offset": 0x12000, "size": 0x1000},
        "parts": parts,
    }
    manifest.update(manifest_overrides or {})
    (bundle_dir / "manifest.json").write_text(json.dumps(manifest))
    (bundle_dir / "sdkconfig.resolved").write_text("CONFIG_BOOTLOADER_APP_ROLLBACK_ENABLE=y\n")
    return bundle_dir


def warnings_in(records: list[logging.LogRecord]) -> list[str]:
    return [record.getMessage() for record in records if record.levelno >= logging.WARNING]
